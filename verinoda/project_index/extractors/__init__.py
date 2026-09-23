"""Per-language extractors, incrementally migrated out of graphify/extract.py.

Dispatch still flows through verinoda.project_index.extract (the facade re-exports every
moved name), so importing from verinoda.project_index.extract keeps working unchanged.
LANGUAGE_EXTRACTORS is the registry seed; wiring dispatch through it is a
later, separate step. See MIGRATION.md for how to port another language.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from verinoda.project_index.extractors.apex import extract_apex
from verinoda.project_index.extractors.bash import extract_bash
from verinoda.project_index.extractors.blade import extract_blade
from verinoda.project_index.extractors.commonlisp import extract_commonlisp
from verinoda.project_index.extractors.dart import extract_dart
from verinoda.project_index.extractors.dm import extract_dm, extract_dmf, extract_dmi, extract_dmm
from verinoda.project_index.extractors.elixir import extract_elixir
from verinoda.project_index.extractors.fortran import extract_fortran
from verinoda.project_index.extractors.go import extract_go
from verinoda.project_index.extractors.json_config import extract_json
from verinoda.project_index.extractors.julia import extract_julia
from verinoda.project_index.extractors.markdown import extract_markdown
from verinoda.project_index.extractors.objc import extract_objc
from verinoda.project_index.extractors.pascal import extract_pascal
from verinoda.project_index.extractors.pascal_forms import extract_delphi_form, extract_lazarus_form
from verinoda.project_index.extractors.powershell import extract_powershell, extract_powershell_manifest
from verinoda.project_index.extractors.razor import extract_razor
from verinoda.project_index.extractors.rust import extract_rust
from verinoda.project_index.extractors.sln import extract_sln
from verinoda.project_index.extractors.sql import extract_sql
from verinoda.project_index.extractors.terraform import extract_terraform
from verinoda.project_index.extractors.verilog import extract_verilog
from verinoda.project_index.extractors.zig import extract_zig

LANGUAGE_EXTRACTORS: dict[str, Callable[[Path], dict]] = {
    "apex": extract_apex,
    "bash": extract_bash,
    "blade": extract_blade,
    "commonlisp": extract_commonlisp,
    "dart": extract_dart,
    "delphi_form": extract_delphi_form,
    "dm": extract_dm,
    "dmf": extract_dmf,
    "dmi": extract_dmi,
    "dmm": extract_dmm,
    "elixir": extract_elixir,
    "fortran": extract_fortran,
    "go": extract_go,
    "json": extract_json,
    "julia": extract_julia,
    "lazarus_form": extract_lazarus_form,
    "markdown": extract_markdown,
    "objc": extract_objc,
    "pascal": extract_pascal,
    "powershell": extract_powershell,
    "powershell_manifest": extract_powershell_manifest,
    "razor": extract_razor,
    "rust": extract_rust,
    "sln": extract_sln,
    "sql": extract_sql,
    "terraform": extract_terraform,
    "verilog": extract_verilog,
    "zig": extract_zig,
}
