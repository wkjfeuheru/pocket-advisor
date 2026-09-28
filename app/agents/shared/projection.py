"""公开 artifact 的深层白名单投影。

机制与领域无关，**schema 与「哪些字段算数字」由调用方注入**：

- 只有出现在 schema 里的字段会被保留，其余一律丢弃（不做浅层删字段）；
- 含数字的对象必须能证明来源：``source`` + 时间（``as_of`` / ``published_at``）。
  证明不了的**整块删除**，因为没法逐字段判断哪些数字可信；
- 静态合约属性（费率、起投金额这类没有披露时间的条款）在 schema 上标记
  ``STATIC``，只要求 ``source``。

``app.agents.stock.evidence`` 使用这一份实现，并提供股票自己的 schema 与数字字段集合。
"""

from __future__ import annotations

from typing import Any

#: schema 节点：``("object", {字段: 子 schema})`` / ``("list", 子 schema)``；
#: 可选第三项是标记字典。
Spec = tuple

#: 标记：该子树的数字只需 ``source``，不要求披露时间。
STATIC = "static"
#: 标记：该对象必须能证明来源，否则**整块**删除（不做「部分可信」）。
REQUIRE_SOURCE = "require_source"


def object_schema(fields: dict[str, Any]) -> Spec:
    return ("object", fields)


def list_schema(item: Spec) -> Spec:
    return ("list", item)


def map_schema(child: Spec | None = None) -> Spec:
    """键名不固定的字典（例如按产品代码索引的取值表）。"""
    return ("map", child)


def static_object_schema(fields: dict[str, Any]) -> Spec:
    """静态合约属性：数字只需 ``source``。"""
    return ("object", fields, {STATIC: True})


def requiring_source_schema(fields: dict[str, Any]) -> Spec:
    """整块必须可溯源；证明不了就整块不公开。"""
    return ("object", fields, {REQUIRE_SOURCE: True})


def _flags(spec: Spec) -> dict[str, Any]:
    if len(spec) > 2 and isinstance(spec[2], dict):
        return spec[2]
    return {}


def _is_static(value: dict[str, Any], spec: Spec) -> bool:
    """静态标记可以来自 schema，也可以来自被投影对象自己的 ``static`` 字段。

    后者用于「同一列表里既有合约条款又有时间序列」的场景（产品解释的各个分节）：
    schema 对列表项只能给一套规则，而分节的静态性由产生它的确定性代码声明。
    """
    return bool(_flags(spec).get(STATIC)) or value.get("static") is True


def _sourced(value: dict[str, Any], spec: Spec, *, inherited: bool) -> bool:
    if inherited:
        return True
    if _is_static(value, spec):
        return bool(value.get("source"))
    timed = value.get("as_of") or value.get("published_at")
    return bool(value.get("source")) and bool(timed)


def _has_number(value: dict[str, Any], fields: dict[str, Any], numeric_fields: set[str]) -> bool:
    return any(
        key in numeric_fields and isinstance(value.get(key), (int, float))
        for key in fields
    )


def project(
    spec: Spec,
    value: Any,
    *,
    numeric_fields: set[str],
    inherited: bool = False,
) -> Any:
    """按 schema 投影；返回 ``None`` 表示这一块整块不可公开。"""
    if spec[0] == "list":
        if not isinstance(value, list):
            return []
        rows = []
        for item in value:
            packed = project(spec[1], item, numeric_fields=numeric_fields, inherited=inherited)
            if packed:
                rows.append(packed)
        return rows

    if spec[0] == "map":
        if not isinstance(value, dict):
            return None
        child = spec[1]
        out_map: dict[str, Any] = {}
        for key, item in value.items():
            if child is None:
                if isinstance(item, (int, float)) and not inherited:
                    continue
                out_map[str(key)] = item
                continue
            packed = project(child, item, numeric_fields=numeric_fields, inherited=inherited)
            if packed is None:
                continue
            out_map[str(key)] = packed
        return out_map or None

    if not isinstance(value, dict):
        return None

    fields = spec[1]
    sourced = _sourced(value, spec, inherited=inherited)
    if _flags(spec).get(REQUIRE_SOURCE) and not sourced:
        return None
    if _has_number(value, fields, numeric_fields) and not sourced:
        return None

    out: dict[str, Any] = {}
    for key, child in fields.items():
        if key not in value:
            continue
        item = value[key]
        if child is None:
            if key in numeric_fields and isinstance(item, (int, float)) and not sourced:
                continue
            out[key] = item
            continue
        packed = project(child, item, numeric_fields=numeric_fields, inherited=sourced)
        if packed is None or packed == {}:
            continue
        out[key] = packed
    return out or None


__all__ = [
    "REQUIRE_SOURCE",
    "STATIC",
    "Spec",
    "list_schema",
    "map_schema",
    "object_schema",
    "project",
    "requiring_source_schema",
    "static_object_schema",
]
