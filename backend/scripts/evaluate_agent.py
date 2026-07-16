from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        cases = payload.get("cases", [])
    else:
        cases = payload
    if not isinstance(cases, list):
        raise ValueError("El fixture debe contener una lista de casos.")
    normalized: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Cada caso debe ser un objeto JSON.")
        normalized.append(case)
    return normalized


def _load_predictions(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "predictions" in payload:
        payload = payload["predictions"]
    if not isinstance(payload, list):
        raise ValueError("Las predicciones deben ser una lista de objetos.")
    result: dict[str, Any] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Cada prediccion debe ser un objeto JSON.")
        case_id = str(item.get("id") or "")
        if not case_id:
            raise ValueError("Cada prediccion debe incluir id.")
        result[case_id] = item.get("actual", item)
    return result


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _compare(expected: Any, actual: Any) -> bool:
    return _normalize(expected) == _normalize(actual)


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    fieldnames = ["id", "match", "expected", "actual"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def run_evaluation(fixture_path: Path, predictions_path: Path | None = None) -> dict[str, Any]:
    cases = _load_cases(fixture_path)
    predictions = _load_predictions(predictions_path) if predictions_path else {}
    rows: list[dict[str, Any]] = []
    exact_matches = 0
    for case in cases:
        case_id = str(case.get("id") or "")
        if not case_id:
            raise ValueError("Cada caso debe incluir id.")
        expected = case.get("expected")
        actual = predictions.get(case_id, case.get("actual"))
        match = _compare(expected, actual)
        exact_matches += int(match)
        rows.append(
            {
                "id": case_id,
                "match": match,
                "expected": json.dumps(_normalize(expected), ensure_ascii=False, sort_keys=True),
                "actual": json.dumps(_normalize(actual), ensure_ascii=False, sort_keys=True),
            }
        )
    total = len(cases)
    summary = {
        "total": total,
        "exact_matches": exact_matches,
        "accuracy": round(exact_matches / total, 4) if total else 0.0,
        "cases": rows,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evalua salidas del agente contra un fixture.")
    parser.add_argument("--fixture", required=True, type=Path, help="Archivo JSON con los casos y esperados.")
    parser.add_argument("--predictions", type=Path, help="Archivo JSON con las predicciones del agente.")
    parser.add_argument("--output", type=Path, help="Ruta opcional para guardar el reporte JSON.")
    parser.add_argument("--csv", dest="csv_output", type=Path, help="Ruta opcional para guardar un CSV.")
    parser.add_argument("--allow-mismatch", action="store_true", help="No devolver error aunque haya diferencias.")
    args = parser.parse_args(argv)

    summary = run_evaluation(args.fixture, args.predictions)
    output = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    if args.csv_output:
        _write_csv(args.csv_output, summary["cases"])
    else:
        print(output)
    if summary["accuracy"] < 1.0 and not args.allow_mismatch:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
