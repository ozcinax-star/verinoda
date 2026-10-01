"""Class files and jars written from scratch for the Java check tests (verinoda/codecheck_java.py): no JDK
and no compiler needed. Methods are abstract or native, so no bytecode is written - unless a method is given
the instructions its ``Code`` attribute holds (the Mixin check reads what they reference)."""
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

    def ref(tag: int, owner: str, mname: str, desc: str) -> int:
        key = ("r", tag, owner, mname, desc)
        if key not in index:
            c = cls(owner)
            nat_key = ("n", mname, desc)
            if nat_key not in index:
                n, d = utf8(mname), utf8(desc)
                pool.append(b"\x0c" + struct.pack(">HH", n, d))
                index[nat_key] = len(pool)
            pool.append(bytes([tag]) + struct.pack(">HH", c, index[nat_key]))
            index[key] = len(pool)
        return index[key]

    def code(items) -> bytes:
        """Bytecode for ``items``: ``(op, owner, name, desc)`` with op one of getfield, putstatic, invokevirtual,
        invokestatic, invokespecial, invokeinterface, new; or "tableswitch", "lookupswitch", "wide" (padding
        and operands the reader must step over). Ends with ``return``."""
        ops = {"getstatic": 0xb2, "putstatic": 0xb3, "getfield": 0xb4, "putfield": 0xb5, "invokevirtual": 0xb6,
               "invokespecial": 0xb7, "invokestatic": 0xb8, "invokeinterface": 0xb9, "new": 0xbb}
        b = bytearray()
        for it in items:
            if it == "tableswitch":
                b += b"\x03\xaa" + b"\x00" * ((-len(b) - 2) % 4)
                b += struct.pack(">iii", 0, 0, 1) + struct.pack(">ii", 0, 0)
            elif it == "lookupswitch":
                b += b"\x03\xab" + b"\x00" * ((-len(b) - 2) % 4)
                b += struct.pack(">ii", 0, 2) + struct.pack(">iiii", 1, 0, 2, 0)
            elif it == "wide":
                b += b"\xc4\x84\x00\x01\x00\x01"
            else:
                op, owner, mname, desc = it
                if op == "new":
                    b += bytes([ops[op]]) + struct.pack(">H", cls(owner))
                    continue
                tag = 9 if op.endswith(("field", "static")) and not op.startswith("invoke") else \
                    11 if op == "invokeinterface" else 10
                b += bytes([ops[op]]) + struct.pack(">H", ref(tag, owner, mname, desc))
                if op == "invokeinterface":
                    b += b"\x01\x00"
        b += b"\xb1"
        return bytes(b)

    this = cls(name)
    sup = cls(super_) if super_ else 0
    iface_ix = [cls(i) for i in ifaces]
    members = []
    code_name = utf8("Code") if any(len(m) > 3 for m in methods) else 0
    for group in (fields, methods):
        out = []
        for row in group:
            mname, desc, acc = row[:3]
            if len(row) > 3:
                body = code(row[3])
                attr = struct.pack(">HHI", 4, 4, len(body)) + body + struct.pack(">HH", 0, 0)
                out.append(struct.pack(">HHHH", acc, utf8(mname), utf8(desc), 1)
                           + struct.pack(">HI", code_name, len(attr)) + attr)
                continue
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
