"""Generate Python code from proto/ into libs/common/src (package ``vitalmesh.events.v1``).

Run from anywhere: ``uv run python scripts/gen_proto.py``. Generated code is gitignored and
rebuilt in CI; never edit it by hand.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import grpc_tools
from grpc_tools import protoc

ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = ROOT / "proto"
OUT_DIR = ROOT / "libs" / "common" / "src"
GENERATED = OUT_DIR / "vitalmesh"


def main() -> int:
    protos = sorted(str(p) for p in PROTO_DIR.rglob("*.proto"))
    if not protos:
        print("no .proto files found", file=sys.stderr)
        return 1
    if GENERATED.exists():
        shutil.rmtree(GENERATED)  # stale generated modules must not linger
    include = Path(grpc_tools.__file__).parent / "_proto"  # google/protobuf/*.proto
    args = [
        "protoc",
        f"-I{PROTO_DIR}",
        f"-I{include}",
        f"--python_out={OUT_DIR}",
        *protos,
    ]
    code = protoc.main(args)
    if code != 0:
        print(f"protoc failed with exit code {code}", file=sys.stderr)
        return code
    count = len(list(GENERATED.rglob("*_pb2.py")))
    print(f"generated {count} modules into {GENERATED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
