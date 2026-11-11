"""CPU timings for the real speaker lookup with an isolated 50k-person snapshot; no LDAP needed."""
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config, corporate_context as cc, ldap_auth, people, store


def main():
    roster = [{"name": f"Фамилия{i} Имя Отчество", "email": f"employee{i}@example.com",
               "job_title": "Аналитик", "department": "ИТ", "surname": f"Фамилия{i}"} for i in range(50000)]
    with tempfile.TemporaryDirectory(prefix="mom-directory-bench-") as temp:
        with patch.object(config, "DATA_DIR", Path(temp)), patch.object(store, "DB_PATH", Path(temp) / "mom.db"), \
             patch.object(config, "ldap_configured", return_value=True), \
             patch.object(config, "get_ldap_base_dn", return_value="DC=example,DC=com"), \
             patch.object(ldap_auth, "search_people", side_effect=AssertionError("Unexpected network lookup")):
            store.init_db()
            cc._update(people=roster, directory_signature=cc.directory_signature(), synced_at=store.utc_now())
            cc.save_search_mode("cache", "benchmark")
            timings = []
            start = time.perf_counter()
            assert people.suggest("фамилия49999")["people"][0]["email"] == "employee49999@example.com"
            cold = (time.perf_counter() - start) * 1000
            for i in range(100):
                query = ["фамилия", "я4999", "employee49999", "имя фамилия49999", "несуществующий"][i % 5]
                start = time.perf_counter()
                result = people.suggest(query)
                timings.append((time.perf_counter() - start) * 1000)
                assert len(result["people"]) <= 20 and "error" not in result
            print(json.dumps({"people": len(roster), "queries": len(timings), "cold_ms": round(cold, 2),
                "warm_median_ms": round(statistics.median(timings), 2),
                "warm_p95_ms": round(sorted(timings)[94], 2), "ldap_requests": 0}, indent=2))


if __name__ == "__main__":
    main()
