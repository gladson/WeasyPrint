# coding: utf8
r"""
    weasyprint.pdf
    --------------

    Post-process the PDF files created by cairo and add metadata such as
    hyperlinks and bookmarks.


    Rather than trying to parse any valid PDF, we make some assumptions
    that hold for cairo in order to simplify the code:

    * All newlines are '\n', not '\r' or '\r\n'
    * Except for number 0 (which is always free) there is no "free" object.
    * Most white space separators are made of a single 0x20 space.
    * Indirect dictionary objects do not contain '>>' at the start of a line
      except to mark the end of the object, followed by 'endobj'.
      (In other words, '>>' markers for sub-dictionaries are indented.)
    * The Page Tree is flat: all kids of the root page node are page objects,
      not page tree nodes.

    However the code uses a lot of assert statements so that if an assumptions
    is not true anymore, the code should (hopefully) fail with an exception
    rather than silently behave incorrectly.


    :copyright: Copyright 2011-2014 Simon Sapin and contributors, see AUTHORS.
    :license: BSD, see LICENSE for details.

"""

from __future__ import division, unicode_literals

import hashlib
import io
import mimetypes
import os
import re
import string
import sys
import zlib

import cairocffi as cairo

from . import VERSION_STRING, Attachment
from .compat import xrange, iteritems, izip
from .urls import iri_to_uri, unquote, urlsplit, URLFetchingError
from .html import W3C_DATE_RE
from .logger import LOGGER


class PDFFormatter(string.Formatter):
    """Like str.format except:

    * Results are byte strings
    * The new !P conversion flags encodes a PDF string.
      (UTF-16 BE with a BOM, then backslash-escape parentheses.)

    Except for fields marked !P, everything should be ASCII-only.

    """
    def convert_field(self, value, conversion):
        if conversion == 'P':
            # Make a round-trip back through Unicode for the .translate()
            # method. (bytes.translate only maps to single bytes.)
            # Use latin1 to map all byte values.
            return '({0})'.format(
                ('\ufeff' + value).encode('utf-16-be').decode('latin1')
                .translate({40: r'\(', 41: r'\)', 92: r'\\'}))
        else:
            return super(PDFFormatter, self).convert_field(value, conversion)

    def vformat(self, format_string, args, kwargs):
        result = super(PDFFormatter, self).vformat(format_string, args, kwargs)
        return result.encode('latin1')

pdf_format = PDFFormatter().format


class PDFDictionary(object):
    def __init__(self, object_number, byte_string):
        self.object_number = object_number
        self.byte_string = byte_string

    def __repr__(self):
        return self.__class__.__name__ + repr(
            (self.object_number, self.byte_string))

    _re_cache = {}

    def get_value(self, key, value_re):
        regex = self._re_cache.get((key, value_re))
        if not regex:
            regex = re.compile(pdf_format('/{0} {1}', key, value_re))
            self._re_cache[key, value_re] = regex
        return regex.search(self.byte_string).group(1)

    def get_type(self):
        """
        :returns: the value for the /Type key.

        """
        # No end delimiter, + defaults to greedy
        return self.get_value('Type', '/(\w+)').decode('ascii')

    def get_indirect_dict(self, key, pdf_file):
        """Read the value for `key` and follow the reference, assuming
        it is an indirect dictionary object.

        :return: a new PDFDictionary instance.

        """
        object_number = int(self.get_value(key, '(\d+) 0 R'))
        return type(self)(object_number, pdf_file.read_object(object_number))

    def get_indirect_dict_array(self, key, pdf_file):
        """Read the value for `key` and follow the references, assuming
        it is an array of indirect dictionary objects.

        :return: a list of new PDFDictionary instance.

        """
        parts = self.get_value(key, '\[(.+?)\]').split(b' 0 R')
        # The array looks like this: ' <a> 0 R <b> 0 R <c> 0 R '
        # so `parts` ends up like this [' <a>', ' <b>', ' <c>', ' ']
        # With the trailing white space in the list.
        trail = parts.pop()
        assert not trail.strip()
        class_ = type(self)
        read = pdf_file.read_object
        return [class_(n, read(n)) for n in map(int, parts)]


class PDFFile(object):
    """
    :param fileobj:
        A seekable binary file-like object for a PDF generated by cairo.

    """
    trailer_re = re.compile(
        b'\ntrailer\n(.+)\nstartxref\n(\d+)\n%%EOF\n$', re.DOTALL)

    def __init__(self, fileobj):
        # cairo’s trailer only has Size, Root and Info.
        # The trailer + startxref + EOF is typically under 100 bytes
        fileobj.seek(-200, os.SEEK_END)
        trailer, startxref = self.trailer_re.search(fileobj.read()).groups()
        trailer = PDFDictionary(None, trailer)
        startxref = int(startxref)

        fileobj.seek(startxref)
        line = next(fileobj)
        assert line == b'xref\n'

        line = next(fileobj)
        first_object, total_objects = line.split()
        assert first_object == b'0'
        total_objects = int(total_objects)

        line = next(fileobj)
        assert line == b'0000000000 65535 f \n'

        objects_offsets = [None]
        for object_number in xrange(1, total_objects):
            line = next(fileobj)
            assert line[10:] == b' 00000 n \n'
            objects_offsets.append(int(line[:10]))

        self.fileobj = fileobj
        #: Maps object number -> bytes from the start of the file
        self.objects_offsets = objects_offsets

        info = trailer.get_indirect_dict('Info', self)
        catalog = trailer.get_indirect_dict('Root', self)
        page_tree = catalog.get_indirect_dict('Pages', self)
        pages = page_tree.get_indirect_dict_array('Kids', self)
        # Check that the tree is flat
        assert all(p.get_type() == 'Page' for p in pages)

        self.startxref = startxref
        self.info = info
        self.catalog = catalog
        self.page_tree = page_tree
        self.pages = pages

        self.finished = False
        self.overwritten_objects_offsets = {}
        self.new_objects_offsets = []

    def read_object(self, object_number):
        """
        :param object_number:
            An integer N so that 1 <= N < len(self.objects_offsets)
        :returns:
            The object content as a byte string.

        """
        fileobj = self.fileobj
        fileobj.seek(self.objects_offsets[object_number])
        line = next(fileobj)
        assert line.endswith(b' 0 obj\n')
        assert int(line[:-7]) == object_number  # len(b' 0 obj\n') == 7
        object_lines = []
        for line in fileobj:
            if line == b'>>\n':
                assert next(fileobj) == b'endobj\n'
                # No newline, we’ll add it when writing.
                object_lines.append(b'>>')
                return b''.join(object_lines)
            object_lines.append(line)

    def overwrite_object(self, object_number, byte_string):
        """Write the new content for an existing object at the end of the file.

        :param object_number:
            An integer N so that 1 <= N < len(self.objects_offsets)
        :param byte_string:
            The new object content as a byte string.

        """
        self.overwritten_objects_offsets[object_number] = (
            self._write_object(object_number, byte_string))

    def extend_dict(self, dictionary, new_content):
        """Overwrite a dictionary object after adding content inside
        the << >> delimiters.

        """
        assert dictionary.byte_string.endswith(b'>>')
        self.overwrite_object(
            dictionary.object_number,
            dictionary.byte_string[:-2] + new_content + b'\n>>')

    def next_object_number(self):
        """Return the object number that would be used by write_new_object().
        """
        return len(self.objects_offsets) + len(self.new_objects_offsets)

    def write_new_object(self, byte_string):
        """Write a new object at the end of the file.

        :param byte_string:
            The object content as a byte string.
        :return:
            The new object number.

        """
        object_number = self.next_object_number()
        self.new_objects_offsets.append(
            self._write_object(object_number, byte_string))
        return object_number

    def finish(self):
        """
        Write the cross-reference table and the trailer for the new and
        overwritten objects. This makes `fileobj` a valid (updated) PDF file.

        """
        new_startxref, write = self._start_writing()
        self.finished = True
        write(b'xref\n')

        # Don’t bother sorting or finding contiguous numbers,
        # just write a new sub-section for each overwritten object.
        for object_number, offset in iteritems(
                self.overwritten_objects_offsets):
            write(pdf_format(
                '{0} 1\n{1:010} 00000 n \n', object_number, offset))

        if self.new_objects_offsets:
            first_new_object = len(self.objects_offsets)
            write(pdf_format(
                '{0} {1}\n', first_new_object, len(self.new_objects_offsets)))
            for object_number, offset in enumerate(
                    self.new_objects_offsets, start=first_new_object):
                write(pdf_format('{0:010} 00000 n \n', offset))

        write(pdf_format(
            'trailer\n<< '
            '/Size {size} /Root {root} 0 R /Info {info} 0 R /Prev {prev}'
            ' >>\nstartxref\n{startxref}\n%%EOF\n',
            size=self.next_object_number(),
            root=self.catalog.object_number,
            info=self.info.object_number,
            prev=self.startxref,
            startxref=new_startxref))

    def _write_object(self, object_number, byte_string):
        offset, write = self._start_writing()
        write(pdf_format('{0} 0 obj\n', object_number))
        write(byte_string)
        write(b'\nendobj\n')
        return offset

    def _start_writing(self):
        assert not self.finished
        fileobj = self.fileobj
        fileobj.seek(0, os.SEEK_END)
        return fileobj.tell(), fileobj.write


def flatten_bookmarks(bookmarks, depth=1):
    for label, target, children in bookmarks:
        yield label, target, depth
        for result in flatten_bookmarks(children, depth + 1):
            yield result


def prepare_metadata(document, bookmark_root_id, scale):
    """Change metadata into data structures closer to the PDF objects.

    In particular, convert from WeasyPrint units (CSS pixels from
    the top-left corner) to PDF units (points from the bottom-left corner.)

    :param scale:
        PDF points per CSS pixels.
        Defaults to 0.75, but is affected by `zoom` in
        :meth:`weasyprint.document.Document.write_pdf`.

    """
    # X and width unchanged;  Y’ = page_height - Y;  height’ = -height
    matrices = [cairo.Matrix(xx=scale, yy=-scale, y0=page.height * scale)
                for page in document.pages]
    links = []
    for page_links, matrix in izip(document.resolve_links(), matrices):
        new_page_links = []
        for link_type, target, rectangle in page_links:
            if link_type == 'internal':
                target_page, target_x, target_y = target
                target = (
                    (target_page,) +
                    matrices[target_page].transform_point(target_x, target_y))
            rect_x, rect_y, width, height = rectangle
            rect_x, rect_y = matrix.transform_point(rect_x, rect_y)
            width, height = matrix.transform_distance(width, height)
            # x, y, w, h => x0, y0, x1, y1
            rectangle = rect_x, rect_y, rect_x + width, rect_y + height
            new_page_links.append((link_type, target, rectangle))
        links.append(new_page_links)

    bookmark_root = {'Count': 0}
    bookmark_list = []
    last_id_by_depth = [bookmark_root_id]
    last_by_depth = [bookmark_root]
    for bookmark_id, (label, target, depth) in enumerate(
            flatten_bookmarks(document.make_bookmark_tree()),
            bookmark_root_id + 1):
        target_page, target_x, target_y = target
        target = (target_page,) + matrices[target_page].transform_point(
            target_x, target_y)
        bookmark = {
            'Count': 0, 'First': None, 'Last': None, 'Prev': None,
            'Next': None, 'Parent': last_id_by_depth[depth - 1],
            'label': label, 'target': target}

        if depth > len(last_by_depth) - 1:
            last_by_depth[depth - 1]['First'] = bookmark_id
        else:
            # The bookmark is sibling of last_id_by_depth[depth]
            bookmark['Prev'] = last_id_by_depth[depth]
            last_by_depth[depth]['Next'] = bookmark_id

            # Remove the bookmarks with a depth higher than the current one
            del last_by_depth[depth:]
            del last_id_by_depth[depth:]

        for i in range(depth):
            last_by_depth[i]['Count'] += 1
        last_by_depth[depth - 1]['Last'] = bookmark_id

        last_by_depth.append(bookmark)
        last_id_by_depth.append(bookmark_id)
        bookmark_list.append(bookmark)
    return bookmark_root, bookmark_list, links


def _write_compressed_file_object(pdf, file):
    """
    Write a file like object as ``/EmbeddedFile``, compressing it with deflate.
    In fact, this method writes multiple PDF objects to include length,
    compressed length and MD5 checksum.

    :return:
        the object number of the compressed file stream object
    """

    object_number = pdf.next_object_number()
    # Make sure we stay in sync with our object numbers
    expected_next_object_number = object_number + 4

    length_number = object_number + 1
    md5_number = object_number + 2
    uncompressed_length_number = object_number + 3

    offset, write = pdf._start_writing()
    write(pdf_format('{0} 0 obj\n', object_number))
    write(pdf_format(
        '<< /Type /EmbeddedFile /Length {0} 0 R /Filter '
        '/FlateDecode /Params << /CheckSum {1} 0 R /Size {2} 0 R >> >>\n',
        length_number, md5_number, uncompressed_length_number))
    write(b'stream\n')

    uncompressed_length = 0
    compressed_length = 0

    md5 = hashlib.md5()
    compress = zlib.compressobj()
    for data in iter(lambda: file.read(4096), b''):
        uncompressed_length += len(data)

        md5.update(data)

        compressed = compress.compress(data)
        compressed_length += len(compressed)

        write(compressed)

    compressed = compress.flush(zlib.Z_FINISH)
    compressed_length += len(compressed)
    write(compressed)

    write(b'\nendstream\n')
    write(b'endobj\n')

    pdf.new_objects_offsets.append(offset)

    pdf.write_new_object(pdf_format("{0}", compressed_length))
    pdf.write_new_object(pdf_format("<{0}>", md5.hexdigest()))
    pdf.write_new_object(pdf_format("{0}", uncompressed_length))

    assert pdf.next_object_number() == expected_next_object_number

    return object_number


def _get_filename_from_result(url, result):
    """
    Derives a filename from a fetched resource. This is either the filename
    returned by the URL fetcher, the last URL path component or a synthetic
    name if the URL has no path
    """

    filename = None

    # A given filename will always take precedence
    if result:
        filename = result.get('filename')
        if filename:
            return filename

    # The URL path likely contains a filename, which is a good second guess
    if url:
        split = urlsplit(url)
        if split.scheme != 'data':
            filename = split.path.split("/")[-1]
            if filename == '':
                filename = None

    if filename is None:
        # The URL lacks a path altogether. Use a synthetic name.

        # Using guess_extension is a great idea, but sadly the extension is
        # probably random, depending on the alignment of the stars, which car
        # you're driving and which software has been installed on your machine.
        #
        # Unfortuneatly this isn't even imdepodent on one machine, because the
        # extension can depend on PYTHONHASHSEED if mimetypes has multiple
        # extensions to offer
        extension = None
        if result:
            mime_type = result.get('mime_type')
            if mime_type == 'text/plain':
                # text/plain has a phletora of extensions - all garbage
                extension = '.txt'
            else:
                extension = mimetypes.guess_extension(mime_type) or '.bin'
        else:
            extension = '.bin'

        filename = 'attachment' + extension
    else:
        if sys.version_info[0] < 3:
            # Python 3 unquotes with UTF-8 per default, here we have to do it
            # manually
            # TODO: this assumes that the filename has been quoted as UTF-8.
            # I'm not sure if this assumption holds, as there is some magic
            # involved with filesystem encoding in other parts of the code
            filename = unquote(filename).encode('latin1').decode('utf-8')
        else:
            filename = unquote(filename)

    return filename


def _write_pdf_embedded_files(pdf, attachments, url_fetcher):
    """
    Writes attachments as embedded files (document attachments).

    :return:
        the object number of the name dictionary or :obj:`None`
    """

    file_spec_ids = []
    for attachment in attachments:
        file_spec_id = _write_pdf_attachment(pdf, attachment, url_fetcher)
        if file_spec_id is not None:
            file_spec_ids.append(file_spec_id)

    # We might have failed to write any attachment at all
    if len(file_spec_ids) == 0:
        return None

    content = [b'<< /Names [']
    for fs in file_spec_ids:
        content.append(pdf_format('\n(attachment{0}) {0} 0 R ',
                       fs))
    content.append(b'\n] >>')
    return pdf.write_new_object(b''.join(content))


def _write_pdf_attachment(pdf, attachment, url_fetcher):
    """
    Writes an attachment to the PDF stream

    :return:
        the object number of the ``/Filespec`` object or :obj:`None` if the
        attachment couldn't be read.
    """
    try:
        # Attachments from document links like <link> or <a> can only be URLs.
        # They're passed in as tuples
        if isinstance(attachment, tuple):
            url, description = attachment
            attachment = Attachment(
                url=url, url_fetcher=url_fetcher, description=description)
        elif not isinstance(attachment, Attachment):
            attachment = Attachment(guess=attachment, url_fetcher=url_fetcher)
    except URLFetchingError as exc:
        LOGGER.warning('Failed to load attachment: %s', exc)
        return None

    with attachment.source as (source_type, source, url, _):
        if isinstance(source, bytes):
            source = io.BytesIO(source)

        file_stream_id = _write_compressed_file_object(pdf, source)

    # TODO: Use the result object from a URL fetch operation to provide more
    # details on the possible filename
    filename = _get_filename_from_result(url, None)

    return pdf.write_new_object(pdf_format(
        '<< /Type /Filespec /F () /UF {0!P} /EF << /F {1} 0 R >> '
        '/Desc {2!P}\n>>',
        filename,
        file_stream_id,
        attachment.description or ''))


def _write_pdf_annotation_files(pdf, links, url_fetcher):
    """
    Write all annotation attachments to the PDF file.

    :return:
        a dictionary that maps URLs to PDF object numbers, which can be
        :obj:`None` if the resource failed to load.
    """
    annot_files = {}
    for page_links in links:
        for link_type, target, rectangle in page_links:
            if link_type == 'attachment' and target not in annot_files:
                annot_files[target] = None
                # TODO: use the title attribute as description
                annot_files[target] = _write_pdf_attachment(
                    pdf, (target, None), url_fetcher)
    return annot_files


def write_pdf_metadata(document, fileobj, scale, metadata, attachments,
                       url_fetcher):
    """Append to a seekable file-like object to add PDF metadata."""
    pdf = PDFFile(fileobj)
    bookmark_root_id = pdf.next_object_number()
    bookmark_root, bookmarks, links = prepare_metadata(
        document, bookmark_root_id, scale)

    if bookmarks:
        pdf.write_new_object(pdf_format(
            '<< /Type /Outlines /Count {0} /First {1} 0 R /Last {2} 0 R\n>>',
            bookmark_root['Count'],
            bookmark_root['First'],
            bookmark_root['Last']))
        for bookmark in bookmarks:
            content = [pdf_format('<< /Title {0!P}\n', bookmark['label'])]
            page_num, pos_x, pos_y = bookmark['target']
            content.append(pdf_format(
                '/A << /Type /Action /S /GoTo '
                '/D [{0} 0 R /XYZ {1:f} {2:f} 0] >>\n',
                pdf.pages[page_num].object_number, pos_x, pos_y))
            if bookmark['Count']:
                content.append(pdf_format('/Count {0}\n', bookmark['Count']))
            for key in ['Parent', 'Prev', 'Next', 'First', 'Last']:
                if bookmark[key]:
                    content.append(pdf_format(
                        '/{0} {1} 0 R\n', key, bookmark[key]))
            content.append(b'>>')
            pdf.write_new_object(b''.join(content))

    embedded_files_id = _write_pdf_embedded_files(
        pdf, metadata.attachments + (attachments or []), url_fetcher)

    if bookmarks or embedded_files_id is not None:
        params = b''
        if bookmarks:
            params += pdf_format(' /Outlines {0} 0 R /PageMode /UseOutlines',
                                 bookmark_root_id)
        if embedded_files_id is not None:
            params += pdf_format(' /Names << /EmbeddedFiles {0} 0 R >>',
                                 embedded_files_id)
        pdf.extend_dict(pdf.catalog, params)

    # A single link can be split in multiple regions. We don't want to embedded
    # a file multiple times of course, so keep a reference to every embedded
    # URL and reuse the object number.
    # TODO: If we add support for descriptions this won't always be correct,
    # because two links might have the same href, but different titles.
    annot_files = _write_pdf_annotation_files(pdf, links, url_fetcher)

    # TODO: splitting a link into multiple independent rectangular annotations
    # works well for pure links, but rather mediocre for other annotations and
    # fails completely for transformed (CSS) or complex link shapes (area).
    # It would be better to use /AP for all links and coalesce link shapes that
    # originate from the same HTML link. This would give a feeling similiar to
    # what browsers do with links that span multiple lines.
    for page, page_links in zip(pdf.pages, links):
        annotations = []
        for link_type, target, rectangle in page_links:
            content = [pdf_format(
                '<< /Type /Annot '
                '/Rect [{0:f} {1:f} {2:f} {3:f}] /Border [0 0 0]\n',
                *rectangle)]
            if link_type != 'attachment' or annot_files[target] is None:
                content.append(b'/Subtype /Link ')
                if link_type == 'internal':
                    content.append(pdf_format(
                        '/A << /Type /Action /S /GoTo '
                        '/D [{0} /XYZ {1:f} {2:f} 0] >>\n',
                        *target))
                else:
                    content.append(pdf_format(
                        '/A << /Type /Action /S /URI /URI ({0}) >>\n',
                        iri_to_uri(target)))
            else:
                assert not annot_files[target] is None

                link_ap = pdf.write_new_object(pdf_format(
                    '<< /Type /XObject /Subtype /Form '
                    '/BBox [{0:f} {1:f} {2:f} {3:f}] /Length 0 >>\n'
                    'stream\n'
                    'endstream',
                    *rectangle))
                content.append(b'/Subtype /FileAttachment ')
                # evince needs /T or fails on an internal assertion. PDF
                # doesn't require it.
                content.append(pdf_format(
                    '/T () /FS {0} 0 R /AP << /N {1} 0 R >>',
                    annot_files[target], link_ap))
            content.append(b'>>')
            annotations.append(pdf.write_new_object(b''.join(content)))

        if annotations:
            pdf.extend_dict(page, pdf_format(
                '/Annots [{0}]', ' '.join(
                    '{0} 0 R'.format(n) for n in annotations)))

    info = [pdf_format('<< /Producer {0!P}\n', VERSION_STRING)]
    for attr, key in (('title', 'Title'), ('description', 'Subject'),
                      ('generator', 'Creator')):
        value = getattr(metadata, attr)
        if value is not None:
            info.append(pdf_format('/{0} {1!P}', key, value))
    for attr, key in (('authors', 'Author'), ('keywords', 'Keywords')):
        value = getattr(metadata, attr)
        if value is not None:
            info.append(pdf_format('/{0} {1!P}', key, ', '.join(value)))
    for attr, key in (('created', 'CreationDate'), ('modified', 'ModDate')):
        value = w3c_date_to_pdf(getattr(metadata, attr), attr)
        if value is not None:
            info.append(pdf_format('/{0} (D:{1})', key, value))
    # TODO: write metadata['CreationDate'] and metadata['ModDate'] as dates.
    info.append(b' >>')
    pdf.overwrite_object(pdf.info.object_number, b''.join(info))

    pdf.finish()


def w3c_date_to_pdf(string, attr_name):
    """
    YYYYMMDDHHmmSSOHH'mm'

    """
    if string is None:
        return None
    match = W3C_DATE_RE.match(string)
    if match is None:
        LOGGER.warning('Invalid %s date: %r', attr_name, string)
        return None
    groups = match.groupdict()
    pdf_date = (groups['year']
                + (groups['month'] or '')
                + (groups['day'] or '')
                + (groups['hour'] or '')
                + (groups['minute'] or '')
                + (groups['second'] or ''))
    if groups['hour']:
        assert groups['minute']
        if not groups['second']:
            pdf_date += '00'
        if groups['tz_hour']:
            assert groups['tz_hour'].startswith(('+', '-'))
            assert groups['tz_minute']
            pdf_date += "%s'%s'" % (groups['tz_hour'], groups['tz_minute'])
        else:
            pdf_date += 'Z'  # UTC
    return pdf_date
