"""CPU-only cost of a maximum-sized vocabulary and a 50,000-person directory."""
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.corporate_context import Context


def main():
    terms = [{"term": f"Система {i}", "aliases": [f"термин{i}"], "description": "Корпоративная система",
              "department": "ИТ" if i % 2 else ""} for i in range(5000)]
    people = [{"name": f"Фамилия{i} Имя Отчество", "surname": f"Фамилия{i}",
               "department": "ИТ", "job_title": "Аналитик", "email": f"{i}@example.com"} for i in range(50000)]
    document = {"segments": [{"text": f"Проверим термин{i % 5000}. Фамилия{i} подготовит отчёт. "
                              "Согласовали сроки и обсудили вопросы."} for i in range(1000)]}
    timings = {"build_ms": [], "hotwords_ms": [], "apply_1000_turns_ms": []}
    for _ in range(10):
        start = time.perf_counter()
        context = Context(terms, people, 1, "1@example.com")
        timings["build_ms"].append((time.perf_counter() - start) * 1000)
        start = time.perf_counter()
        assert len(context.hotwords("термин4999").encode()) <= 500
        timings["hotwords_ms"].append((time.perf_counter() - start) * 1000)
        start = time.perf_counter()
        result = context.apply(document)
        assert len(result["corporate_context"]["entries"]) == 30
        timings["apply_1000_turns_ms"].append((time.perf_counter() - start) * 1000)
    print(json.dumps({"terms": len(terms), "people": len(people), "turns": len(document["segments"]),
                      "median_ms": {name: statistics.median(values) for name, values in timings.items()},
                      "measurements": timings}, indent=2))


if __name__ == "__main__":
    main()
