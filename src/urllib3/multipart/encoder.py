"""This holds all of the implementation details of the MultipartEncoder."""
import contextlib
import io
import os
import typing

from .. import _collections, fields, filepost

P = typing.TypeVar("P", bound="Part")


class Part:
    def __init__(
        self,
        headers: bytes,
        body: typing.Union[typing.BinaryIO, "FileWrapper", "_CustomBytesIO"],
    ) -> None:
        self.headers: bytes = headers
        self.body: typing.Union[typing.BinaryIO, "FileWrapper", "_CustomBytesIO"] = body
        self.headers_unread = True
        self.len = len(self.headers) + total_len(self.body)

    @classmethod
    def from_field(cls: typing.Type[P], field: fields.RequestField, encoding: str) -> P:
        """Create a part from a Request Field generated by urllib3."""
        headers = encode_with(field.render_headers(), encoding)
        body = coerce_data(field.data, encoding)
        return cls(headers, body)

    def bytes_left_to_write(self) -> int:
        """Determine if there are bytes left to write.

        :returns: bool -- ``True`` if there are bytes left to write, otherwise
            ``False``
        """
        to_read = 0
        if self.headers_unread:
            to_read += len(self.headers)

        return (to_read + total_len(self.body)) > 0

    def write_to(self, buffer: "_CustomBytesIO", size: int) -> int:
        """Write the requested amount of bytes to the buffer provided.

        The number of bytes written may exceed size on the first read since we
        load the headers ambitiously.

        :param _CustomBytesIO buffer: buffer we want to write bytes to
        :param int size: number of bytes requested to be written to the buffer
        :returns: int -- number of bytes actually written
        """
        written = 0
        if self.headers_unread:
            written += buffer.append(self.headers)
            self.headers_unread = False

        while total_len(self.body) > 0 and (size == -1 or written < size):
            amount_to_read = size
            if size != -1:
                amount_to_read = size - written
            written += buffer.append(self.body.read(amount_to_read))

        return written


class _CustomBytesIO(io.BytesIO):
    def __init__(
        self,
        buffer: typing.Optional[typing.Union[typing.BinaryIO, str, bytes]] = None,
        encoding: str = "utf-8",
    ) -> None:
        if buffer is None:
            buffer = b""
        if isinstance(buffer, typing.BinaryIO):
            bufferbytes = buffer.read()
        else:
            bufferbytes = encode_with(buffer, encoding)
        super().__init__(bufferbytes)

    def _get_end(self) -> int:
        current_pos = self.tell()
        self.seek(0, 2)
        length = self.tell()
        self.seek(current_pos, 0)
        return length

    @property
    def len(self) -> int:
        length = self._get_end()
        return length - self.tell()

    def append(self, bytes: bytes) -> int:
        with reset(self):
            written = self.write(bytes)
        return written

    def smart_truncate(self) -> None:
        to_be_read = total_len(self)
        already_read = self._get_end() - to_be_read

        if already_read >= to_be_read:
            old_bytes = self.read()
            self.seek(0, 0)
            self.truncate()
            self.write(old_bytes)
            self.seek(0, 0)  # We want to be at the beginning


class FileWrapper:
    def __init__(self, file_object: typing.BinaryIO):
        self.fd = file_object
        self._total_len = total_len(self.fd)

    @property
    def len(self) -> int:
        return self._total_len - self.fd.tell()

    def read(self, length: int = -1) -> bytes:
        return self.fd.read(length)


PartTuples = typing.Union[
    typing.Tuple[
        str,
        typing.Union[bytes, str, typing.BinaryIO],
    ],
    typing.Tuple[
        str,
        typing.Union[bytes, str, typing.BinaryIO],
        typing.Union[bytes, str],
    ],
    typing.Tuple[
        str,
        typing.Union[bytes, str, typing.BinaryIO],
        typing.Union[bytes, str],
        typing.Mapping[str, str],
    ],
]
Fields = typing.Union[
    typing.Mapping[str, typing.Union[bytes, str, PartTuples, typing.BinaryIO]],
    typing.Sequence[
        typing.Tuple[str, typing.Union[bytes, str, PartTuples, typing.BinaryIO]]
    ],
]


class MultipartEncoder:
    """A memory-efficient way of streaming large files in multipart/form-data format.

    The basic usage is:

    .. code-block:: python

        import urllib3
        from urllib3.multipart import MultipartEncoder

        pm = urllib3.PoolManager()
        encoder = MultipartEncoder({'field': 'value',
                                    'other_field', 'other_value'})
        r = pm.urlopen(
            method='POST',
            url='https://httpbin.org/post',
            body=encoder,
            headers=encoder.headers,
        )

    If you do not need to take advantage of streaming the post body, you can
    also do:

    .. code-block:: python

        import urllib3
        from urllib3.multipart import MultipartEncoder

        pm = urllib3.PoolManager()
        encoder = MultipartEncoder({'field': 'value',
                                    'other_field', 'other_value'})
        r = pm.urlopen(
            method='POST',
            url='https://httpbin.org/post',
            body=encoder.read(),
            headers=encoder.headers,
        )

    If you want the encoder to use a specific order, you can use an
    OrderedDict or more simply, a list of tuples:

    .. code-block:: python

        encoder = MultipartEncoder([('field', 'value'),
                                    ('other_field', 'other_value')])

    You can also provide tuples as part values in the same formats as are
    supported by :meth:`~urllib3.fields.RequestField.from_tuples`

    .. code-block:: python

        encoder = MultipartEncoder({
            'field': ('file_name', b'{"a": "b"}', 'application/json',
                      {'X-My-Header': 'my-value'})
        ])

    Finally, you can also optionally specify the boundary string to use.
    """

    def __init__(
        self,
        fields: Fields,
        boundary: typing.Optional[str] = None,
        encoding: str = "utf-8",
        default_iter_size: int = 8192 * 4,
    ):
        self._boundary_value: str = boundary or filepost.choose_boundary()
        self._boundary: str = f"--{self._boundary_value}"
        self._enc: str = encoding
        # Pre-encoded boundary
        self._encoded_boundary: bytes = (
            encode_with(self._boundary + "\r\n", self._enc),
        )
        self._fields = fields
        self._iter_read_size = default_iter_size
        self._finished: bool = False
        # The part we're currently working with
        self._current_part: typing.Optional[Part] = None
        # Cached computation of the body's length
        self._len: typing.Optional[int] = None
        # Our buffer
        self._buffer = _CustomBytesIO(encoding=encoding)
        # Pre-compute each part's headers
        self._parts, self._iter_parts = self._prepare_parts()
        # Load boundary into buffer
        self._write_boundary()

    @property
    def boundary(self) -> str:
        """Computed boundary."""
        return self._boundary

    @property
    def boundary_value(self) -> str:
        """Boundary value either passed in by the user or created."""
        return self._boundary_value

    @property
    def default_iter_read_size(self) -> str:
        """Default amount to read when used as an iterator."""
        return self._iter_read_size

    @property
    def encoding(self) -> str:
        """Encoding of the data being passed in."""
        return self._enc

    @property
    def fields(self) -> Fields:
        """Fields provided by the user."""
        return self._fields

    @property
    def finished(self) -> bool:
        """Whether the encoder has been consumed."""
        return self._finished

    def __iter__(self) -> typing.Iterator[bytes]:
        while not self._finished:
            yield self.read(self._iter_read_size)

    def __next__(self) -> bytes:
        if self._finished:
            raise StopIteration()
        return self.read(self._iter_read_size)

    def __len__(self) -> int:
        """Length of the multipart/form-data body."""
        # If _len isn't already calculated, calculate, return, and set it
        return self._len or self._calculate_length()

    def __repr__(self) -> str:
        return f"<MultipartEncoder: {self._fields!r}>"

    def _calculate_length(self) -> int:
        """
        This uses the parts to calculate the length of the body.

        This returns the calculated length so __len__ can be lazy.
        """
        boundarycrnl_len = len(self._boundary) + len("\r\n\r\n")
        self._len = sum(total_len(p) for p in self._parts) + (
            boundarycrnl_len * (len(self._parts) + 1)
        )
        return self._len

    def _calculate_load_amount(self, read_size: int) -> int:
        """This calculates how many bytes need to be added to the buffer.

        When a consumer read's ``x`` from the buffer, there are two cases to
        satisfy:

            1. Enough data in the buffer to return the requested amount
            2. Not enough data

        This function uses the amount of unread bytes in the buffer and
        determines how much the Encoder has to load before it can return the
        requested amount of bytes.

        :param int read_size: the number of bytes the consumer requests
        :returns: int -- the number of bytes that must be loaded into the
            buffer before the read can be satisfied. This will be strictly
            non-negative
        """
        amount = read_size - total_len(self._buffer)
        return amount if amount > 0 else 0

    def _load(self, amount: int) -> None:
        """Load ``amount`` number of bytes into the buffer."""
        self._buffer.smart_truncate()
        part = self._current_part or self._next_part()
        while amount == -1 or amount > 0:
            written = 0
            if part and not part.bytes_left_to_write():
                written += self._write(b"\r\n")
                written += self._write_boundary()
                part = self._next_part()

            if not part:
                written += self._write_closing_boundary()
                self._finished = True
                break

            written += part.write_to(self._buffer, amount)

            if amount != -1:
                amount -= written

    def _next_part(self) -> typing.Optional[Part]:
        try:
            p = self._current_part = next(self._iter_parts)
        except StopIteration:
            return None
        return p

    def _iter_fields(self) -> typing.Generator[fields.RequestField, None, None]:
        _fields = self._fields
        if hasattr(self._fields, "items"):
            self._fields = typing.cast(
                typing.Mapping[
                    str, typing.Union[bytes, str, PartTuples, typing.BinaryIO]
                ],
                self._fields,
            )
            _fields = list(self._fields.items())
        _fields = typing.cast(
            typing.Sequence[
                typing.Tuple[str, typing.Union[bytes, str, PartTuples, typing.BinaryIO]]
            ],
            _fields,
        )
        for k, v in _fields:
            file_name = None
            file_type = None
            file_headers = None
            if isinstance(v, (list, tuple)):
                if len(v) == 2:
                    v = typing.cast(
                        typing.Tuple[
                            str,
                            typing.Union[bytes, str, typing.BinaryIO],
                        ],
                        v,
                    )
                    file_name, file_pointer = v
                elif len(v) == 3:
                    v = typing.cast(
                        typing.Tuple[
                            str,
                            typing.Union[bytes, str, typing.BinaryIO],
                            typing.Union[bytes, str],
                        ],
                        v,
                    )
                    file_name, file_pointer, file_type = v
                else:
                    v = typing.cast(
                        typing.Tuple[
                            str,
                            typing.Union[bytes, str, typing.BinaryIO],
                            typing.Union[bytes, str],
                            typing.Mapping[str, str],
                        ],
                        v,
                    )
                    file_name, file_pointer, file_type, file_headers = v
            else:
                file_pointer = v

            field = fields.RequestField(
                name=k, data=file_pointer, filename=file_name, headers=file_headers
            )
            if isinstance(file_type, bytes):
                file_type = file_type.decode("utf-8")
            field.make_multipart(content_type=file_type)
            yield field

    def _prepare_parts(self) -> typing.Tuple[typing.List[Part], typing.Iterator[Part]]:
        """This uses the fields provided by the user and creates Part objects.

        It populates the `parts` attribute and uses that to create a
        generator for iteration.
        """
        enc = self._enc
        parts = [Part.from_field(f, enc) for f in self._iter_fields()]
        return parts, iter(parts)

    def _write(self, bytes_to_write: typing.Union[bytes, bytearray]) -> int:
        """Write the bytes to the end of the buffer.

        :param bytes bytes_to_write: byte-string (or bytearray) to append to
            the buffer
        :returns: int -- the number of bytes written
        """
        return self._buffer.append(bytes_to_write)

    def _write_boundary(self) -> int:
        """Write the boundary to the end of the buffer."""
        return self._write(self._encoded_boundary)

    def _write_closing_boundary(self) -> int:
        """Write the bytes necessary to finish a multipart/form-data body."""
        with reset(self._buffer):
            self._buffer.seek(-2, 2)
            self._buffer.write(b"--\r\n")
        return 2

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self._boundary_value}"

    @property
    def content_length(self) -> str:
        return f"{len(self)}"

    @property
    def headers(self) -> _collections.HTTPHeaderDict:
        return _collections.HTTPHeaderDict(
            {
                "Content-Type": self.content_type,
                "Content-Length": self.content_length,
            }
        )

    def read(self, size: int = -1) -> bytes:
        """Read data from the streaming encoder.

        :param int size: (optional), If provided, ``read`` will return exactly
            that many bytes. If it is not provided, it will return the
            remaining bytes.
        :returns: bytes
        """
        if self._finished:
            return self._buffer.read(size)

        bytes_to_load = size
        if bytes_to_load != -1 and bytes_to_load is not None:
            bytes_to_load = self._calculate_load_amount(int(size))

        self._load(bytes_to_load)
        return self._buffer.read(size)


@typing.overload
def encode_with(string: bytes, encoding: str) -> bytes:
    ...


@typing.overload
def encode_with(string: None, encoding: str) -> None:
    ...


@typing.overload
def encode_with(string: str, encoding: str) -> bytes:
    ...


def encode_with(
    string: typing.Optional[typing.Union[str, bytes]], encoding: str
) -> typing.Optional[bytes]:
    """Encoding ``string`` with ``encoding`` if necessary.

    :param str string: If string is a bytes object, it will not encode it.
        Otherwise, this function will encode it with the provided encoding.
    :param str encoding: The encoding with which to encode string.
    :returns: encoded bytes object
    """
    if not (string is None or isinstance(string, bytes)):
        return string.encode(encoding)
    return string


def readable_data(
    data: typing.Union[typing.AnyStr, typing.BinaryIO], encoding: str
) -> typing.Union[typing.BinaryIO, _CustomBytesIO]:
    """Coerce the data to an object with a ``read`` method."""
    if hasattr(data, "read"):
        data = typing.cast(typing.BinaryIO, data)
        return data

    data = typing.cast(typing.AnyStr, data)
    return _CustomBytesIO(data, encoding)


@typing.overload
def total_len(o: typing.TextIO) -> int:
    ...


@typing.overload
def total_len(o: typing.BinaryIO) -> int:
    ...


@typing.overload
def total_len(o: str) -> int:
    ...


@typing.overload
def total_len(o: bytes) -> int:
    ...


@typing.overload
def total_len(o: FileWrapper) -> int:
    ...


@typing.overload
def total_len(o: Part) -> int:
    ...


@typing.overload
def total_len(o: typing.Sized) -> int:
    ...


def total_len(
    o: typing.Union[
        Part, FileWrapper, typing.AnyStr, typing.TextIO, typing.BinaryIO, typing.Sized
    ]
) -> int:
    if hasattr(o, "__len__"):
        o = typing.cast(typing.Union[typing.Sized, typing.AnyStr], o)
        return len(o)

    if hasattr(o, "len"):
        o = typing.cast(typing.Union[Part, FileWrapper, _CustomBytesIO], o)
        return o.len

    if hasattr(o, "fileno"):
        o = typing.cast(typing.Union[typing.TextIO, typing.BinaryIO], o)
        try:
            fileno = o.fileno()
        except io.UnsupportedOperation:
            pass
        else:
            return os.fstat(fileno).st_size

    if hasattr(o, "getvalue"):
        o = typing.cast(typing.Union[io.BytesIO, io.StringIO], o)
        # e.g. BytesIO, cStringIO.StringIO
        return len(o.getvalue())

    raise ValueError("Unable to compute size", o)


@contextlib.contextmanager
def reset(buffer: typing.BinaryIO) -> typing.Iterator[None]:
    """Keep track of the buffer's current position and write to the end.

    This is a context manager meant to be used when adding data to the buffer.
    It eliminates the need for every function to be concerned with the
    position of the cursor in the buffer.
    """
    original_position = buffer.tell()
    buffer.seek(0, 2)
    yield
    buffer.seek(original_position, 0)


@typing.overload
def coerce_data(data: typing.BinaryIO, encoding: str) -> FileWrapper:
    ...


@typing.overload
def coerce_data(data: str, encoding: str) -> _CustomBytesIO:
    ...


@typing.overload
def coerce_data(data: bytes, encoding: str) -> _CustomBytesIO:
    ...


def coerce_data(
    data: typing.Union[_CustomBytesIO, io.BytesIO, typing.BinaryIO, str, bytes],
    encoding: str,
) -> typing.Union[_CustomBytesIO, FileWrapper]:
    """Ensure that every object's __len__ behaves uniformly."""
    if not isinstance(data, _CustomBytesIO):
        if isinstance(data, io.BytesIO):
            return _CustomBytesIO(data.getvalue(), encoding)

        if isinstance(data, (str, bytes)):
            return _CustomBytesIO(data, encoding)

        if hasattr(data, "fileno"):
            return FileWrapper(data)

    data = typing.cast(_CustomBytesIO, data)
    return data


def to_list(
    fields: Fields,
) -> typing.List[
    typing.Tuple[str, typing.Union[bytes, str, PartTuples, typing.BinaryIO]]
]:
    if hasattr(fields, "items"):
        fields = typing.cast(
            typing.Mapping[str, typing.Union[bytes, str, PartTuples, typing.BinaryIO]],
            fields,
        )
        return list(fields.items())

    fields = typing.cast(
        typing.Sequence[
            typing.Tuple[str, typing.Union[bytes, str, PartTuples, typing.BinaryIO]]
        ],
        fields,
    )
    return list(fields)
