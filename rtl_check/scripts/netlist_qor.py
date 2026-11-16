"""Extract hierarchy-aware netlist QoR, latches, memories, dead-logic, and fanout from Yosys JSON."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

OPERATORS = {
    "multiply": {"$mul"},
    "add_sub": {"$add", "$sub"},
    "compare": {"$eq", "$ne", "$eqx", "$nex", "$lt", "$le", "$gt", "$ge"},
    "shift": {"$shl", "$shr", "$sshl", "$sshr", "$shift", "$shiftx"},
    "divide_mod": {"$div", "$mod", "$divfloor", "$modfloor"},
    "mux": {"$mux", "$pmux"},
}

CLOCK_PIN_NAMES = {"CLK", "C", "CK", "CLK_I", "RD_CLK", "WR_CLK"}


def _strip_slash(value: str) -> str:
    return value[1:] if value.startswith("\\") else value


def _demangle_module_name(module_key: str, module_obj: dict) -> str:
    attrs = module_obj.get("attributes") or {}
    hdlname = str(attrs.get("hdlname") or "").strip()
    if hdlname:
        return "::".join(part.lstrip("\\") for part in hdlname.split())
    clean = _strip_slash(module_key)
    if clean.startswith("$paramod"):
        parts = [p for p in clean.split("\\") if p]
        if len(parts) >= 2:
            return parts[1]
    return clean


def _integer(params: dict, key: str, default: int = 0) -> int:
    value = params.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"Yosys parameter {key} is boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[01]+", text):
            return int(text, 2)
        try:
            return int(text, 0)
        except ValueError as exc:
            raise ValueError(f"cannot decode Yosys parameter {key}={value!r}") from exc
    raise ValueError(f"unsupported Yosys parameter {key} type: {type(value).__name__}")


def _operation(cell_type: str) -> str | None:
    for name, types in OPERATORS.items():
        if cell_type in types:
            return name
    return None


def _memory_cell(cell_name: str, cell: dict) -> dict:
    params = cell.get("parameters", {})
    width = _integer(params, "WIDTH")
    depth = _integer(params, "SIZE")
    read_ports = _integer(params, "RD_PORTS")
    write_ports = _integer(params, "WR_PORTS")
    clock_mask = _integer(params, "RD_CLK_ENABLE")
    if read_ports <= 0:
        latency = None
    elif clock_mask == 0:
        latency = 0
    elif clock_mask == (1 << read_ports) - 1:
        latency = 1
    else:
        latency = None
    mem_id = _strip_slash(str(params.get("MEMID", cell_name))).lstrip("\\")
    return {
        "name": mem_id,
        "cell_type": cell.get("type"),
        "width": width,
        "depth": depth,
        "bits": width * depth,
        "read_ports": read_ports,
        "write_ports": write_ports,
        "read_latency": latency,
    }


def _find_top_key(modules: dict, top: str) -> str:
    module_keys = {_strip_slash(name): name for name in modules}
    top_key = module_keys.get(_strip_slash(top))
    if top_key is None:
        for raw_key, mod_obj in modules.items():
            if _demangle_module_name(raw_key, mod_obj) == _strip_slash(top):
                top_key = raw_key
                break
    if top_key is None:
        raise ValueError(f"Top module {top!r} not found in Yosys JSON")
    return top_key


def summarize_netlist(path: Path, top: str) -> dict:
    data = json.loads(path.read_text())
    modules = data.get("modules")
    if not isinstance(modules, dict):
        raise ValueError("Yosys JSON has no modules mapping")

    module_keys = {_strip_slash(name): name for name in modules}
    top_key = _find_top_key(modules, top)
    top_clean = _demangle_module_name(top_key, modules[top_key])

    totals = {
        "top": top_clean,
        "cell_count": 0,
        "ff_bits": 0,
        "latch_bits": 0,
        "memory_objects": 0,
        "memory_bits": 0,
        "mux_data_bits": 0,
        "operator_groups": {name: 0 for name in OPERATORS},
        "cells_by_type": Counter(),
        "operator_inventory": [],
        "memory_inventory": [],
        "latch_inventory": [],
    }

    def entry(detail: dict, module_name: str, instance: str) -> dict:
        item_path = f"{instance}/{detail['name']}"
        return {
            **detail,
            "id": item_path,
            "path": item_path,
            "module": module_name,
            "instance": instance,
        }

    def visit(module_key: str, instance: str, stack: tuple[str, ...]) -> None:
        if module_key in stack:
            raise ValueError("recursive module hierarchy: " + " -> ".join(stack + (module_key,)))
        module = modules[module_key]
        module_name = _demangle_module_name(module_key, module)
        seen_mem_names: set[str] = set()

        for cell_name, cell in module.get("cells", {}).items():
            cell_type = str(cell.get("type", "unknown"))
            child = module_keys.get(_strip_slash(cell_type))
            if child is not None:
                visit(child, f"{instance}/{_strip_slash(cell_name)}", stack + (module_key,))
                continue

            totals["cell_count"] += 1
            totals["cells_by_type"][cell_type] += 1
            params = cell.get("parameters", {})
            width = _integer(params, "WIDTH", _integer(params, "Y_WIDTH", 1))
            lowered = cell_type.lower()

            if "latch" in lowered or cell_type == "$sr":
                totals["latch_bits"] += width
                totals["latch_inventory"].append(
                    entry({"name": _strip_slash(cell_name), "cell_type": cell_type, "width": width},
                          module_name, instance)
                )
            elif "dff" in lowered or cell_type.startswith("$_DFF"):
                totals["ff_bits"] += width

            if cell_type in {"$mux", "$pmux"}:
                totals["mux_data_bits"] += width * max(1, _integer(params, "S_WIDTH", 1))

            op = _operation(cell_type)
            if op is not None:
                totals["operator_groups"][op] += 1
                detail = {
                    "name": _strip_slash(cell_name),
                    "cell_type": cell_type,
                    "operation": op,
                    "a_width": _integer(params, "A_WIDTH"),
                    "b_width": _integer(params, "B_WIDTH"),
                    "y_width": _integer(params, "Y_WIDTH", width),
                }
                totals["operator_inventory"].append(entry(detail, module_name, instance))

            if cell_type.startswith("$mem") and not cell_type.startswith(("$memrd", "$memwr", "$meminit")):
                detail = _memory_cell(cell_name, cell)
                seen_mem_names.add(detail["name"])
                totals["memory_objects"] += 1
                totals["memory_bits"] += detail["bits"]
                totals["memory_inventory"].append(entry(detail, module_name, instance))

        for memory_name, memory in module.get("memories", {}).items():
            clean_mem = _strip_slash(memory_name).lstrip("\\")
            if clean_mem in seen_mem_names:
                continue
            width = int(memory.get("width", 0) or 0)
            depth = int(memory.get("size", 0) or 0)
            detail = {
                "name": clean_mem,
                "cell_type": "memory",
                "width": width,
                "depth": depth,
                "bits": width * depth,
                "read_ports": None,
                "write_ports": None,
                "read_latency": None,
            }
            totals["memory_objects"] += 1
            totals["memory_bits"] += detail["bits"]
            totals["memory_inventory"].append(entry(detail, module_name, instance))

    visit(top_key, top_clean, ())
    totals["ff_bits_estimate"] = totals["ff_bits"]
    totals["mux_data_bits_estimate"] = totals["mux_data_bits"]
    totals["cells_by_type"] = dict(sorted(totals["cells_by_type"].items()))
    totals["operator_inventory"].sort(key=lambda item: item["path"])
    totals["memory_inventory"].sort(key=lambda item: item["path"])
    totals["latch_inventory"].sort(key=lambda item: item["path"])
    return totals


def analyze_mapped_netlist(path: Path, top: str) -> dict:
    """Extract stuck/undriven top output bits and non-clock signal fanout from techmapped JSON."""
    data = json.loads(path.read_text())
    modules = data.get("modules")
    if not isinstance(modules, dict):
        raise ValueError("Mapped Yosys JSON has no modules mapping")

    top_key = _find_top_key(modules, top)
    mod = modules[top_key]

    stuck_output_bits = 0
    undriven_output_bits = 0
    stuck_outputs: list[dict] = []
    for port_name, port in (mod.get("ports") or {}).items():
        if port.get("direction") not in {"output", "inout"}:
            continue
        bits = port.get("bits") or []
        const_bits = [b for b in bits if b in {"0", "1"}]
        xz_bits = [b for b in bits if b in {"x", "z"}]
        stuck_output_bits += len(const_bits)
        undriven_output_bits += len(xz_bits)
        if const_bits or xz_bits:
            stuck_outputs.append({
                "port": _strip_slash(port_name),
                "total_bits": len(bits),
                "const_bits": len(const_bits),
                "xz_bits": len(xz_bits),
                "sample": "".join(str(b) for b in bits[:8]),
            })

    bit_to_name: dict[int, str] = {}
    for net_name, net in (mod.get("netnames") or {}).items():
        if net.get("hide_name"):
            continue
        clean_name = _strip_slash(net_name)
        for bit in net.get("bits") or []:
            if isinstance(bit, int) and bit not in bit_to_name:
                bit_to_name[bit] = clean_name
    for net_name, net in (mod.get("netnames") or {}).items():
        clean_name = _strip_slash(net_name)
        for bit in net.get("bits") or []:
            if isinstance(bit, int) and bit not in bit_to_name:
                bit_to_name[bit] = clean_name

    clock_bits: set[int] = set()
    for port_name, port in (mod.get("ports") or {}).items():
        if port.get("direction") == "input" and "clk" in port_name.lower():
            for bit in port.get("bits") or []:
                if isinstance(bit, int):
                    clock_bits.add(bit)

    bit_loads: Counter[int] = Counter()
    for cell in (mod.get("cells") or {}).values():
        port_dirs = cell.get("port_directions") or {}
        conns = cell.get("connections") or {}
        for pname, bits in conns.items():
            if port_dirs.get(pname, "input") == "output":
                continue
            if pname.upper() in CLOCK_PIN_NAMES:
                continue
            for bit in bits:
                if isinstance(bit, int) and bit not in clock_bits:
                    bit_loads[bit] += 1

    net_max: dict[str, int] = defaultdict(int)
    for bit, count in bit_loads.items():
        nname = bit_to_name.get(bit, f"net_{bit}")
        if count > net_max[nname]:
            net_max[nname] = count

    ranked = sorted(net_max.items(), key=lambda kv: (-kv[1], kv[0]))
    fanout_top = [{"net": k, "fanout": v} for k, v in ranked[:5]]
    max_fanout = ranked[0][1] if ranked else 0

    return {
        "stuck_output_bits": stuck_output_bits,
        "undriven_output_bits": undriven_output_bits,
        "stuck_outputs": stuck_outputs,
        "max_fanout": max_fanout,
        "fanout_top": fanout_top,
    }
