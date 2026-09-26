"""Class files and jars written from scratch for the Java check tests (verinoda/codecheck_java.py): no JDK
and no compiler needed. Methods are abstract or native, so no bytecode is written."""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

PUBLIC, STATIC, NATIVE, ABSTRACT, VARARGS, INTERFACE = 0x0001, 0x0008, 0x0100, 0x0400, 0x0080, 0x0200


def class_bytes(name: str, *, super_: str | None = "java/lang/Object", ifaces: tuple[str, ...] = (),
                methods: tuple[tuple[str, str, int], ...] = (), fields: tuple[tuple[str, str, int], ...] = (),
                flags: int = PUBLIC | 0x0020) -> bytes:
    """A class file: ``methods`` and ``fields`` are ``(name, descriptor, access flags)``."""
    pool: list[bytes] = []
    index: dict[tuple, int] = {}

    def utf8(s: str) -> int:
        key = ("u", s)
        if key not in index:
            b = s.encode("utf-8")
            pool.append(b"\x01" + struct.pack(">H", len(b)) + b)
            index[key] = len(pool)
        return index[key]

    def cls(s: str) -> int:
        key = ("c", s)
        if key not in index:
            n = utf8(s)
            pool.append(b"\x07" + struct.pack(">H", n))
            index[key] = len(pool)
        return index[key]

    this = cls(name)
    sup = cls(super_) if super_ else 0
    iface_ix = [cls(i) for i in ifaces]
    members = []
    for group in (fields, methods):
        out = []
        for mname, desc, acc in group:
            if group is methods and not acc & (ABSTRACT | NATIVE):
                acc |= NATIVE  # no Code attribute to write
            out.append(struct.pack(">HHHH", acc, utf8(mname), utf8(desc), 0))
        members.append(out)
    body = struct.pack(">HHH", flags, this, sup) + struct.pack(">H", len(iface_ix))
    body += b"".join(struct.pack(">H", i) for i in iface_ix)
    for out in members:
        body += struct.pack(">H", len(out)) + b"".join(out)
    body += struct.pack(">H", 0)  # class attributes
    return b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 52) + struct.pack(">H", len(pool) + 1) + b"".join(pool) + body


def write_jar(path: Path, classes: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in classes.items():
            z.writestr(name + ".class", data)
    return path


def base_classes() -> dict[str, bytes]:
    """java.lang.Object, String and Enum as the check needs them (a test never depends on the machine's JDK)."""
    return {
        "java/lang/Object": class_bytes("java/lang/Object", super_=None, methods=(
            ("<init>", "()V", PUBLIC), ("toString", "()Ljava/lang/String;", PUBLIC),
            ("equals", "(Ljava/lang/Object;)Z", PUBLIC), ("hashCode", "()I", PUBLIC))),
        "java/lang/String": class_bytes("java/lang/String", methods=(
            ("<init>", "()V", PUBLIC), ("trim", "()Ljava/lang/String;", PUBLIC), ("length", "()I", PUBLIC),
            ("substring", "(I)Ljava/lang/String;", PUBLIC), ("substring", "(II)Ljava/lang/String;", PUBLIC),
            ("format", "(Ljava/lang/String;[Ljava/lang/Object;)Ljava/lang/String;", PUBLIC | STATIC | VARARGS))),
        "java/lang/Enum": class_bytes("java/lang/Enum", methods=(
            ("name", "()Ljava/lang/String;", PUBLIC), ("ordinal", "()I", PUBLIC))),
        "java/lang/Record": class_bytes("java/lang/Record"),
    }
