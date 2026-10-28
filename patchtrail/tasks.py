"""The small authored task catalog. Public metadata never includes scripted solutions."""
from dataclasses import dataclass
from pathlib import Path


CHECK_COMMAND = ("python", "-I", "-B", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v")
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True, slots=True)
class TaskSpec:
    id: str
    title: str
    description: str
    source_dir: Path
    editable_files: tuple[str, ...]
    check_command: tuple[str, ...]
    demo_path: str
    demo_content: str
    tags: tuple[str, ...]
    private: bool = False
    project_id: str = ""
    snapshot_id: str = ""
    check_profile: str = "unittest"
    test_paths: tuple[str, ...] = ()
    source_hash: str = ""


_QUANTITY_SOLUTION = '"""Shopping quantities use a fixed unit per ingredient (for example grams)."""\nimport math\n\n\ndef _validate(values):\n    for name, quantity in values.items():\n        if not isinstance(name, str) or not name.strip():\n            raise ValueError("ingredient names must be non-empty strings")\n        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):\n            raise ValueError("quantity must be numeric")\n        if not math.isfinite(quantity) or quantity < 0:\n            raise ValueError("quantity must be finite and non-negative")\n\n\ndef shopping_shortfall(required, stock):\n    """Return strictly positive quantities still required for a meal plan."""\n    _validate(required)\n    _validate(stock)\n    result = {}\n    for ingredient, amount in required.items():\n        missing = max(0, amount - stock.get(ingredient, 0))\n        if missing > 0:\n            result[ingredient] = missing\n    return result\n'
_BATCH_SOLUTION = '"""Keep fixed-unit ingredient quantities with their own batch expiry dates."""\nfrom datetime import date\n\n\ndef _quantity(value):\n    if type(value) is not int or value <= 0:\n        raise ValueError("quantity must be a positive integer")\n\n\ndef add_batch(batches, name, quantity, expires_on):\n    """Return new inventory without changing the caller\'s batch records."""\n    if not isinstance(name, str) or not name.strip():\n        raise ValueError("name is required")\n    name = name.strip().casefold()\n    _quantity(quantity)\n    date.fromisoformat(expires_on)\n    result = [dict(batch) for batch in batches]\n    for batch in result:\n        if batch["name"].strip().casefold() == name and batch["expires_on"] == expires_on:\n            batch["quantity"] += quantity\n            batch["expires_on"] = expires_on\n            return result\n    result.append({"name": name, "quantity": quantity, "expires_on": expires_on})\n    return result\n'
_LABEL_SOLUTION = '"""Update a sample label and append its audit history."""\nimport sqlite3\n\n\ndef update_label(database_path, sample_id, label, note=""):\n    if label not in {"ham", "spam"}:\n        raise ValueError("label must be ham or spam")\n    connection = sqlite3.connect(database_path)\n    try:\n        connection.execute("BEGIN IMMEDIATE")\n        row = connection.execute("SELECT label FROM samples WHERE id = ?", (sample_id,)).fetchone()\n        if row is None:\n            raise LookupError("sample not found")\n        old_label = row[0]\n        if old_label == label:\n            return {"changed": False, "label": label}\n        connection.execute("UPDATE samples SET label = ? WHERE id = ?", (label, sample_id))\n        connection.execute(\n            "INSERT INTO history(sample_id, old_label, new_label, note) VALUES (?, ?, ?, ?)",\n            (sample_id, old_label, label, note),\n        )\n        connection.commit()\n        return {"changed": True, "label": label}\n    except Exception:\n        connection.rollback()\n        raise\n    finally:\n        connection.close()\n'

_TASKS = (
    TaskSpec("quantity-shortfall", "库存数量缺口", "购物清单把已有食材误当成数量足够。修复固定单位下的缺口计算，保留输入校验与原输入。", FIXTURES / "quantity-shortfall", ("src/quantities.py",), CHECK_COMMAND, "src/quantities.py", _QUANTITY_SOLUTION, ("business-logic", "quantity", "regression")),
    TaskSpec("batch-expiry", "补货批次到期", "新增补货错误地延长旧批次的到期日。保留不同到期日期的独立批次，仅合并相同食材且相同到期日的数量。", FIXTURES / "batch-expiry", ("src/batches.py",), CHECK_COMMAND, "src/batches.py", _BATCH_SOLUTION, ("state", "batch", "expiry")),
    TaskSpec("label-transaction", "标签与历史原子性", "标签已提交而历史写入失败，导致数据与审计记录不一致。修复事务边界，同时保留相同标签无改动和未知样本错误。", FIXTURES / "label-transaction", ("src/labels.py",), CHECK_COMMAND, "src/labels.py", _LABEL_SOLUTION, ("sqlite", "transaction", "rollback")),
)


def get_task(task_id: str) -> TaskSpec:
    for task in _TASKS:
        if task.id == task_id:
            return task
    raise KeyError(task_id)


def list_tasks() -> list[dict]:
    from .workspace import tree_hash
    return [{"id": task.id, "title": task.title, "description": task.description,
             "editable_files": list(task.editable_files), "check_id": "tests",
             "tags": list(task.tags), "source_hash": tree_hash(task.source_dir)}
            for task in _TASKS]
