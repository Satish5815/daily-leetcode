from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from openai import OpenAI

ROOT = Path(__file__).parent
HISTORY_PATH = ROOT / "data" / "history.json"
SOLUTIONS_DIR = ROOT / "solutions"
REPORTS_DIR = ROOT / "reports"
LEETCODE_GRAPHQL = "https://leetcode.com/graphql"
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"
COUNT = max(2, min(3, int(os.getenv("PROBLEMS_PER_DAY", "3"))))
DIFFICULTIES = [x.strip().upper() for x in os.getenv(
    "DIFFICULTIES", "EASY,MEDIUM,HARD"
).split(",") if x.strip()]

client = OpenAI()


def load_history() -> dict:
    if not HISTORY_PATH.exists():
        return {"completed": [], "attempted": []}
    return json.loads(HISTORY_PATH.read_text())


def save_history(history: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=2) + "\n")


def fetch_problems() -> list[dict]:
    query = """
    query problemsetQuestionList($categorySlug: String, $limit: Int, $skip: Int, $filters: QuestionListFilterInput) {
      problemsetQuestionList(
        categorySlug: $categorySlug, limit: $limit, skip: $skip, filters: $filters
      ) {
        questions {
          questionFrontendId title titleSlug difficulty paidOnly
          topicTags { name }
        }
      }
    }
    """
    problems = []
    for skip in range(0, 2000, 100):
        response = requests.post(
            LEETCODE_GRAPHQL,
            json={"query": query, "variables": {
                "categorySlug": "", "skip": skip, "limit": 100,
                "filters": {"difficulty": None, "status": None, "tags": []}
            }},
            headers={"Content-Type": "application/json", "Referer": "https://leetcode.com/problemset/"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        questions = payload.get("data", {}).get("problemsetQuestionList", {}).get("questions", [])
        if not questions:
            break
        problems.extend(q for q in questions if not q.get("paidOnly"))
        if len(questions) < 100:
            break
    if not problems:
        raise RuntimeError("LeetCode returned no problems; the public GraphQL schema may have changed.")
    return problems


def select_problems(problems: list[dict], history: dict) -> list[dict]:
    done = {str(x) for x in history.get("completed", [])}
    attempted = {str(x) for x in history.get("attempted", [])}
    seen = done | attempted
    candidates = [
        p for p in problems
        if str(p.get("questionFrontendId")) not in seen
        and p.get("difficulty", "").upper() in DIFFICULTIES
        and p.get("titleSlug")
    ]
    if len(candidates) < COUNT:
        raise RuntimeError("Not enough unseen problems found for the requested daily count.")

    # Prefer a mixed set when all three difficulty buckets are enabled.
    selected = []
    for difficulty in DIFFICULTIES:
        bucket = [p for p in candidates if p["difficulty"].upper() == difficulty]
        if bucket and len(selected) < COUNT:
            selected.append(random.choice(bucket))
    remaining = [p for p in candidates if p not in selected]
    while len(selected) < COUNT and remaining:
        p = random.choice(remaining)
        selected.append(p)
        remaining.remove(p)
    return selected


def fetch_detail(slug: str) -> dict:
    query = """
    query questionData($titleSlug: String!) {
      question(titleSlug: $titleSlug) {
        questionId questionFrontendId title titleSlug content difficulty
        codeSnippets { lang langSlug code }
        exampleTestcases
      }
    }
    """
    response = requests.post(
        LEETCODE_GRAPHQL,
        json={"query": query, "variables": {"titleSlug": slug}},
        headers={"Content-Type": "application/json", "Referer": f"https://leetcode.com/problems/{slug}/"},
        timeout=30,
    )
    response.raise_for_status()
    question = response.json().get("data", {}).get("question")
    if not question:
        raise RuntimeError(f"Could not fetch details for {slug}")
    return question


def generate_solution(problem: dict) -> str:
    snippets = problem.get("codeSnippets") or []
    python_snippet = next((s.get("code", "") for s in snippets if s.get("langSlug") == "python3"), "")
    if not python_snippet:
        python_snippet = next((s.get("code", "") for s in snippets if s.get("langSlug") == "python"), "")

    prompt = f"""
You are a careful LeetCode Python solver. Return ONLY a complete Python solution file.
Use the exact LeetCode method/class signature from the starter snippet. Do not include
Markdown fences, prose, input parsing, or a main block. Prefer an efficient, correct
algorithm. Do not use external packages. Problem: {problem.get('title')}
ID: {problem.get('questionFrontendId')}
Difficulty: {problem.get('difficulty')}
Statement HTML:
{problem.get('content', '')}
Official Python starter:
{python_snippet}
"""
    response = client.responses.create(
        model=MODEL,
        input=prompt,
        temperature=0.1,
    )
    code = response.output_text.strip()
    code = re.sub(r"^```(?:python)?\s*", "", code)
    code = re.sub(r"\s*```$", "", code)
    if "class Solution" not in code:
        raise RuntimeError("AI output did not contain a LeetCode Solution class.")
    compile(code, "<generated-solution>", "exec")
    return code + "\n"


def run_command(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )


def create_starter(problem_id: str, slug: str) -> Path:
    before = set(ROOT.glob("*.py"))
    result = run_command(["leetcode", "create", problem_id], timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"leetcode create failed: {result.stderr[-1500:]}")
    after = set(ROOT.glob("*.py"))
    new_files = list(after - before)
    if new_files:
        return new_files[0]
    # CLI versions may create files in a configured directory; search narrowly.
    matches = list(ROOT.glob(f"**/{problem_id}*.py"))
    matches = [p for p in matches if ".venv" not in str(p) and "site-packages" not in str(p)]
    if matches:
        return max(matches, key=lambda p: p.stat().st_mtime)
    raise RuntimeError(
        "Could not locate the file created by `leetcode create`. "
        "Check the installed CLI version and update create_starter()."
    )


def process_problem(problem: dict, history: dict, report: list[dict]) -> None:
    problem_id = str(problem["questionFrontendId"])
    slug = problem["titleSlug"]
    title = problem["title"]
    url = f"https://leetcode.com/problems/{slug}/"
    item = {"id": problem_id, "title": title, "difficulty": problem["difficulty"], "url": url}
    print(f"Processing #{problem_id} {title} ({problem['difficulty']})")

    try:
        detail = fetch_detail(slug)
        code = generate_solution(detail)
        SOLUTIONS_DIR.mkdir(parents=True, exist_ok=True)
        solution_path = SOLUTIONS_DIR / f"{problem_id}.{slug}.py"
        solution_path.write_text(code)

        # The CLI test command uses LeetCode's example test cases.
        test_result = run_command(["leetcode", "test", str(solution_path)], timeout=240)
        test_output = (test_result.stdout + "\n" + test_result.stderr).strip()
        (REPORTS_DIR / f"{problem_id}.{slug}.test.log").write_text(test_output + "\n")

        # Fail closed: CLI versions differ, so require an explicit success marker.
        explicit_pass = bool(re.search(
            r"(all test cases passed|all tests passed|accepted|passed all test cases)",
            test_output, re.IGNORECASE
        ))
        explicit_fail = bool(re.search(
            r"(wrong answer|time limit exceeded|runtime error|compile error|failed|error:)",
            test_output, re.IGNORECASE
        ))
        if test_result.returncode != 0 or not explicit_pass or explicit_fail:
            item.update(
                status="TEST_FAILED_OR_UNCONFIRMED",
                details=test_output[-1200:],
            )
            history.setdefault("attempted", []).append(problem_id)
            report.append(item)
            return

        item["tests"] = "CLI emitted an explicit success marker; inspect test log to confirm test counts."
        if DRY_RUN:
            item["status"] = "DRY_RUN_TEST_PASSED"
            report.append(item)
            return

        submit_result = run_command(["leetcode", "submit", str(solution_path)], timeout=300)
        submit_output = (submit_result.stdout + "\n" + submit_result.stderr).strip()
        (REPORTS_DIR / f"{problem_id}.{slug}.submit.log").write_text(submit_output + "\n")
        if submit_result.returncode == 0:
            item.update(status="SUBMITTED_CHECK_RESULT", details=submit_output[-1200:])
            # Do not mark as completed solely from a zero exit code; CLI output
            # may indicate Wrong Answer or Pending. Verify the report/result.
            if re.search(r"\bAccepted\b", submit_output, re.IGNORECASE):
                history.setdefault("completed", []).append(problem_id)
            else:
                history.setdefault("attempted", []).append(problem_id)
        else:
            item.update(status="SUBMIT_FAILED", details=submit_output[-1200:])
            history.setdefault("attempted", []).append(problem_id)
        report.append(item)
    except Exception as exc:
        item.update(status="ERROR", details=f"{type(exc).__name__}: {exc}")
        history.setdefault("attempted", []).append(problem_id)
        report.append(item)


def main() -> int:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    history = load_history()
    problems = fetch_problems()
    selected = select_problems(problems, history)
    report: list[dict] = []

    for problem in selected:
        process_problem(problem, history, report)
        save_history(history)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"daily-report-{stamp}.json"
    report_path.write_text(json.dumps({
        "date_utc": stamp,
        "dry_run": DRY_RUN,
        "model": MODEL,
        "results": report,
    }, indent=2) + "\n")

    print("\nDaily summary:")
    for item in report:
        print(f"- {item['status']}: #{item['id']} {item['title']} — {item['url']}")
    print(f"Report: {report_path}")
    return 0 if report and all(x["status"] in {"DRY_RUN_TEST_PASSED", "SUBMITTED_CHECK_RESULT"} for x in report) else 1


if __name__ == "__main__":
    sys.exit(main())
