"""Small reproducible platform experiments; this is not application acceptance."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import sqlite3
import tempfile
import unicodedata


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def python_strings(_directory):
    whitespace = {repr(c): c.isspace() for c in [" ", "\t", "\n", "\u00a0"]}
    require(all(whitespace.values()), "Expected whitespace classification")
    value = "\tАнна\u00a0"
    require((" " + value + " ").strip(" ") == value, "strip must remove only U+0020")
    accepted = ["00123", "+79991234567", "1", "9" * 15]
    rejected = ["", "+", "1" * 16, "123abc", "123\n", "١٢٣", "１２３", "1-23"]
    for value in accepted:
        require(re.fullmatch(r"[+]?[0-9]{1,15}", value) is not None, repr(value))
    for value in rejected:
        require(re.fullmatch(r"[+]?[0-9]{1,15}", value) is None, repr(value))
    return {"whitespace": whitespace, "accepted_phones": accepted, "rejected_phones": rejected}


def schema_transaction(directory):
    path = directory / "schema.sqlite3"
    with closing(sqlite3.connect(path, autocommit=True)) as db:
        journal = db.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        db.execute("PRAGMA synchronous=FULL")
        sync = db.execute("PRAGMA synchronous").fetchone()[0]
        require(journal == "delete" and sync == 2, "Unexpected connection settings")
        for finish in ["ROLLBACK", "COMMIT"]:
            db.execute("BEGIN")
            db.execute("PRAGMA application_id=0x50484431")
            db.execute("PRAGMA user_version=1")
            db.execute("CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "name TEXT NOT NULL COLLATE BINARY, phone TEXT NOT NULL COLLATE BINARY)")
            db.execute("CREATE INDEX records_name ON records(name)")
            db.execute("CREATE INDEX records_phone ON records(phone)")
            db.execute(finish)
            if finish == "ROLLBACK":
                require(db.execute("PRAGMA application_id").fetchone()[0] == 0, "Marker rollback")
                require(db.execute("PRAGMA user_version").fetchone()[0] == 0, "Version rollback")
                require(not db.execute("SELECT name FROM sqlite_schema WHERE name='records'").fetchall(),
                        "Schema rollback")
    with closing(sqlite3.connect(path, autocommit=True)) as db:
        require(db.execute("PRAGMA application_id").fetchone()[0] == 0x50484431, "Marker persisted")
        require(db.execute("PRAGMA user_version").fetchone()[0] == 1, "Version persisted")
        require(len(db.execute("PRAGMA table_info(records)").fetchall()) == 3, "Columns persisted")
        require(len(db.execute("PRAGMA index_list(records)").fetchall()) == 2, "Indexes persisted")
        db.autocommit = False
        db.execute("INSERT INTO records(name, phone) VALUES (?, ?)", ("Rollback", "1"))
        db.rollback()
        require(db.execute("SELECT count(*) FROM records").fetchone()[0] == 0, "Method rollback")
        db.execute("INSERT INTO records(name, phone) VALUES (?, ?)", ("Commit", "2"))
        db.commit()
    with closing(sqlite3.connect(path, autocommit=True)) as db:
        require(db.execute("SELECT name, phone FROM records").fetchall() == [("Commit", "2")],
                "Method commit persisted")
    return {"journal_mode": journal, "synchronous": sync, "schema_and_markers_rollback": True,
            "schema_and_markers_persist": True, "commit_rollback_with_autocommit_false": True}


def comparison_and_ids(directory):
    path = directory / "values.sqlite3"
    with closing(sqlite3.connect(path, autocommit=True)) as db:
        db.execute("CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "name TEXT NOT NULL COLLATE BINARY, phone TEXT NOT NULL COLLATE BINARY)")
        db.execute("CREATE INDEX records_name ON records(name)")
        values = [("Анна", "00123"), ("Анна", "00123"), ("анна", "+123"),
                  ("é", "1"), ("e\u0301", "2")]
        for value in values:
            db.execute("INSERT INTO records(name, phone) VALUES (?, ?)", value)
        count = db.execute("SELECT count(*) FROM records WHERE name=?", ("Анна",)).fetchone()[0]
        require(count == 2, "Exact comparison or duplicates differ")
        require(db.execute("SELECT count(*) FROM records WHERE name=?", ("é",)).fetchone()[0] == 1,
                "Unexpected Unicode normalization")
        require(db.execute("SELECT phone, typeof(phone) FROM records WHERE id=1").fetchone()
                == ("00123", "text"), "Leading zeros or TEXT affinity")
        require(db.execute("SELECT count(DISTINCT id) FROM records").fetchone()[0] == len(values),
                "IDs must distinguish duplicates")
        db.execute("DELETE FROM records WHERE id=5")
        next_id = db.execute("INSERT INTO records(name, phone) VALUES (?, ?)", ("Борис", "3")).lastrowid
        require(next_id > 5, "AUTOINCREMENT reused a committed deleted ID")
        db.execute("INSERT INTO records(name, phone) VALUES (?, ?)", ("Numeric", 123))
        require(db.execute("SELECT typeof(phone) FROM records WHERE name='Numeric'").fetchone()[0]
                == "text", "TEXT affinity conversion")
        plan = [row[3] for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM records WHERE name=?", ("Анна",))]
        require(any("records_name" in row for row in plan), "Expected index in this query plan")
    return {"exact_name_matches": count, "leading_zeros_preserved": True,
            "canonical_unicode_forms_distinct": True, "id_after_deleted_5": next_id,
            "numeric_input_converted_to_text": True, "query_plan": plan,
            "note": "Deletion is a platform experiment, not a proposed application operation."}


def full_database(directory):
    with closing(sqlite3.connect(directory / "full.sqlite3", autocommit=True)) as db:
        db.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, value BLOB)")
        db.execute("INSERT INTO evidence VALUES (1, ?)", (b"old",))
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        original_limit = db.execute("PRAGMA max_page_count").fetchone()[0]
        db.execute(f"PRAGMA max_page_count={pages}")
        db.execute("BEGIN")
        error_name = None
        try:
            db.execute("INSERT INTO evidence VALUES (2, zeroblob(1048576))")
            db.execute("COMMIT")
        except sqlite3.DatabaseError as error:
            error_name = error.sqlite_errorname
            if db.in_transaction:
                db.execute("ROLLBACK")
        require(error_name == "SQLITE_FULL", f"Expected SQLITE_FULL, got {error_name}")
        require(db.execute("SELECT id, value FROM evidence").fetchall() == [(1, b"old")],
                "Previously committed row changed")
        db.execute(f"PRAGMA max_page_count={original_limit}")
        db.execute("INSERT INTO evidence VALUES (2, zeroblob(1048576))")
        require(db.execute("SELECT count(*) FROM evidence").fetchone()[0] == 2, "Recovery write failed")
    return {"error": error_name, "old_data_unchanged": True, "write_after_limit_restored": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    report = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "unicode": unicodedata.unidata_version, "os": platform.system(),
                        "os_release": platform.release(), "architecture": platform.machine()},
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Temporary platform experiments only; no application, crash, HTTP or performance acceptance.",
        "checks": [],
    }
    with tempfile.TemporaryDirectory(prefix="phone-directory-facts-") as temporary:
        for check in [python_strings, schema_transaction, comparison_and_ids, full_database]:
            try:
                details = check(Path(temporary))
                report["checks"].append({"name": check.__name__, "status": "passed", "details": details})
            except Exception as error:
                report["checks"].append({"name": check.__name__, "status": "failed", "error": str(error)})
    report["status"] = "passed" if all(c["status"] == "passed" for c in report["checks"]) else "failed"
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
