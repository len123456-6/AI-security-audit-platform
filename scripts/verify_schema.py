"""schema.sql 自检脚本（开发工具，不属于应用代码）。

用法::

    python scripts/verify_schema.py

作用：在内存 SQLite 中执行 schema.sql，验证
  1. SQL 语法可执行（含触发器、视图、部分索引）且可重复执行（幂等）
  2. 各项约束真实生效（CHECK / UNIQUE / 外键 / 单基准食材）
  3. updated_at 触发器、ON DELETE CASCADE 工作正常
  4. 成本视图与 ratio 换算能算出结果

任何一条不满足即以非 0 退出码结束，便于将来放进 CI。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "schema.sql"


class CheckFailed(AssertionError):
    """断言失败。"""


def expect_error(conn: sqlite3.Connection, sql: str, label: str) -> None:
    """执行一条「应当被拒绝」的语句，被拒绝即通过。"""
    try:
        conn.execute(sql)
    except sqlite3.IntegrityError:
        print(f"  [OK] 约束生效: {label}")
        return
    raise CheckFailed(f"约束未生效（语句被意外接受）: {label}")


def _make_stdout_safe() -> None:
    """避免输出被重定向时因编码问题崩溃。

    Windows 下把 stdout 重定向到文件时，Python 会用系统 ANSI 代码页（中文系统是 GBK），
    遇到 GBK 无法表示的字符会抛 UnicodeEncodeError 并中断整个自检。
    这里只把错误处理策略改成 replace，不动编码，终端显示保持原样。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def main() -> int:
    _make_stdout_safe()
    if not SCHEMA_FILE.is_file():
        print(f"[FAIL] 找不到 {SCHEMA_FILE}")
        return 1

    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        # ---- 1. 可执行 + 幂等 ----
        conn.executescript(sql)
        print(f"[OK] schema.sql 执行成功（{len(sql.splitlines())} 行）")
        conn.executescript(sql)
        print("[OK] 重复执行不报错（IF NOT EXISTS / INSERT OR IGNORE）")

        # ---- 2. 对象清单 ----
        def names(kind: str, exclude_internal: bool = False) -> set[str]:
            cond = " AND name NOT LIKE 'sqlite_%'" if exclude_internal else ""
            return {
                r["name"]
                for r in conn.execute(
                    f"SELECT name FROM sqlite_master WHERE type = '{kind}'{cond}"
                )
            }

        tables = names("table", exclude_internal=True)
        missing = {"recipes", "ingredients", "recipe_ingredients"} - tables
        if missing:
            raise CheckFailed(f"缺少表: {sorted(missing)}")
        print(f"[OK] 表: {sorted(tables)}")

        if "v_recipe_cost" not in names("view"):
            raise CheckFailed("缺少视图 v_recipe_cost")

        indexes = names("index")
        for name in ("uk_ingredients_name", "idx_ri_recipe", "uk_ri_single_base"):
            if name not in indexes:
                raise CheckFailed(f"缺少索引 {name}")
        print(f"[OK] 视图 v_recipe_cost；索引 {len(indexes)} 个（含部分唯一索引 uk_ri_single_base）")

        triggers = names("trigger")
        if {"trg_recipes_updated_at", "trg_ingredients_updated_at"} - triggers:
            raise CheckFailed("updated_at 触发器缺失")
        print(f"[OK] 触发器: {sorted(triggers)}")

        # ---- 3. 外键开关 ----
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise CheckFailed("PRAGMA foreign_keys 未启用")
        print("[OK] PRAGMA foreign_keys = ON")

        # ---- 4. 种子数据 ----
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("recipes", "ingredients", "recipe_ingredients")
        }
        if counts["recipes"] != 2 or counts["recipe_ingredients"] != 12:
            raise CheckFailed(f"种子数据数量异常: {counts}")
        print(f"[OK] 种子数据: {counts}")

        # ---- 5. 约束逐条验证 ----
        expect_error(
            conn,
            "INSERT INTO recipes (title, difficulty) VALUES ('超难菜', 9)",
            "difficulty 必须在 1..5",
        )
        expect_error(
            conn,
            "INSERT INTO recipes (title, servings) VALUES ('零人份', 0)",
            "servings 必须 > 0",
        )
        expect_error(
            conn,
            "INSERT INTO recipes (title, source) VALUES ('来源非法', 'crawler')",
            "source 只能是 manual/ai",
        )
        expect_error(
            conn,
            "INSERT INTO ingredients (name, category) VALUES ('榴莲', '水果蔬菜')",
            "ingredients.category 白名单",
        )
        expect_error(
            conn,
            "INSERT INTO ingredients (name) VALUES ('番茄')",
            "食材名唯一（uk_ingredients_name）",
        )
        expect_error(
            conn,
            "INSERT INTO recipe_ingredients (recipe_id, ingredient_id) VALUES (1, 1)",
            "同食谱+食材+分组不可重复（uk_recipe_ingredient）",
        )
        expect_error(
            conn,
            "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit, ratio, is_base) "
            "VALUES (1, 8, 10, '克', 1.0, 1)",
            "每个食谱只能有一个基准食材（uk_ri_single_base）",
        )
        expect_error(
            conn,
            "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, ratio, is_base) "
            "VALUES (1, 9, 0.5, 1)",
            "基准食材必须给出 ratio",
        )
        expect_error(
            conn,
            "INSERT INTO recipe_ingredients (recipe_id, ingredient_id) VALUES (1, 9)",
            "quantity 与 unit 不能同时为空",
        )
        expect_error(
            conn,
            "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit) "
            "VALUES (999, 1, 10, '克')",
            "外键 recipe_id -> recipes.id",
        )
        expect_error(
            conn,
            "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit) "
            "VALUES (1, 999, 10, '克')",
            "外键 ingredient_id -> ingredients.id",
        )

        # ---- 6. 级联删除（SAVEPOINT 内验证后回滚，避免污染后续断言）----
        conn.execute("SAVEPOINT cascade_check")
        conn.execute("DELETE FROM recipes WHERE id = 2")
        orphan = conn.execute(
            "SELECT COUNT(*) FROM recipe_ingredients WHERE recipe_id = 2"
        ).fetchone()[0]
        if orphan:
            raise CheckFailed("ON DELETE CASCADE 未生效，留下孤立配料行")
        conn.execute("ROLLBACK TO cascade_check")
        conn.execute("RELEASE cascade_check")
        still_there = conn.execute("SELECT COUNT(*) FROM recipes WHERE id = 2").fetchone()[0]
        if still_there != 1:
            raise CheckFailed("SAVEPOINT 回滚失败，测试数据未还原")
        print("[OK] ON DELETE CASCADE：删除食谱时自动清理配料行（已回滚，数据还原）")

        # ---- 7. updated_at 触发器 ----
        before = conn.execute("SELECT updated_at FROM recipes WHERE id = 1").fetchone()[0]
        time.sleep(0.02)
        conn.execute("UPDATE recipes SET title = '番茄炒鸡蛋' WHERE id = 1")
        after = conn.execute("SELECT updated_at FROM recipes WHERE id = 1").fetchone()[0]
        if after <= before:
            raise CheckFailed(f"updated_at 未被触发器刷新: {before} -> {after}")
        print(f"[OK] updated_at 触发器: {before} -> {after}")

        # ---- 8. 成本视图（同时守住「元/default_unit」口径，防止单位填错）----
        row = conn.execute(
            "SELECT title, servings, ingredient_rows, required_rows, estimated_cost "
            "FROM v_recipe_cost WHERE recipe_id = 1"
        ).fetchone()
        expected_cost = round(
            300 * 0.012     # 番茄 300 克
            + 3 * 1.200     # 鸡蛋 3 个
            + 20 * 0.010    # 食用油 20 毫升
            + 3 * 0.003     # 盐 3 克
            + 5 * 0.006,    # 白砂糖 5 克
            2,
        )
        if row["ingredient_rows"] != 6 or row["required_rows"] != 4:
            raise CheckFailed(f"v_recipe_cost 行数统计错误: {dict(row)}")
        if abs(row["estimated_cost"] - expected_cost) > 0.01:
            raise CheckFailed(f"预估成本不符: {row['estimated_cost']} != {expected_cost}")
        # 合理的两人份家常菜成本区间：0.5 ~ 50 元。低于/高于都说明价格口径写错了单位
        if not 0.5 <= row["estimated_cost"] <= 50:
            raise CheckFailed(
                f"预估成本 {row['estimated_cost']} 元不合理，"
                "检查 ingredients.reference_price 是否按 default_unit 填写"
            )
        print(
            f"[OK] v_recipe_cost: {row['title']} 配料 {row['ingredient_rows']} 行 / "
            f"必需 {row['required_rows']} 行 / 预估 {row['estimated_cost']:.2f} 元"
            f"（{row['servings']} 人份，成本口径合理）"
        )

        # ---- 9. ratio 数据质量：只有与基准同单位的行才允许写 ratio ----
        bad = conn.execute(
            """
            SELECT ri.recipe_id, i.name, ri.unit, base.unit AS base_unit
              FROM recipe_ingredients ri
              JOIN recipe_ingredients base
                ON base.recipe_id = ri.recipe_id AND base.is_base = 1
              JOIN ingredients i ON i.id = ri.ingredient_id
             WHERE ri.ratio IS NOT NULL
               AND ri.unit IS NOT NULL
               AND ri.unit <> base.unit
            """
        ).fetchall()
        if bad:
            raise CheckFailed(f"跨单位行填了 ratio（无意义数据）: {[dict(b) for b in bad]}")
        checked = conn.execute(
            """
            SELECT ri.recipe_id, ri.quantity, ri.ratio, base.quantity AS base_qty
              FROM recipe_ingredients ri
              JOIN recipe_ingredients base
                ON base.recipe_id = ri.recipe_id AND base.is_base = 1
             WHERE ri.ratio IS NOT NULL AND ri.is_base = 0
            """
        ).fetchall()
        for c in checked:
            derived = round(c["base_qty"] * c["ratio"], 4)
            if abs(derived - c["quantity"]) > 0.01:
                raise CheckFailed(
                    f"recipe {c['recipe_id']}: ratio {c['ratio']} × 基准 {c['base_qty']} "
                    f"= {derived}，但 quantity 记的是 {c['quantity']}"
                )
        print(f"[OK] ratio 数据质量：跨单位 ratio 为 0 条，同单位 ratio 自洽 {len(checked)} 条")

        # ---- 10. 份数换算：2 人份 -> 4 人份 ----
        scale = 4 / 2
        egg = conn.execute(
            "SELECT quantity FROM recipe_ingredients "
            "WHERE recipe_id = 1 AND ingredient_id = 2 AND unit = '个'"
        ).fetchone()
        potato = conn.execute(
            "SELECT quantity FROM recipe_ingredients "
            "WHERE recipe_id = 2 AND is_base = 1"
        ).fetchone()
        print(
            f"[OK] 份数换算 ×{scale:g}: 鸡蛋 {egg['quantity']:g} 个 -> {egg['quantity'] * scale:g} 个；"
            f"土豆 {potato['quantity']:g} 克 -> {potato['quantity'] * scale:g} 克"
        )
    except (CheckFailed, sqlite3.Error) as exc:
        print(f"[FAIL] {exc}")
        return 1
    finally:
        conn.close()

    print("\n全部检查通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())

