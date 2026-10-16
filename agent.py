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


def generate_solution(problem: dict, language: str) -> str:
    snippets = problem.get("codeSnippets") or []
    language_config = {
        "python": {
            "slugs": ("python3", "python"),
            "extension": "py",
            "name": "Python",
        },
        "javascript": {
            "slugs": ("javascript",),
            "extension": "js",
            "name": "JavaScript",
        },
    }[language]
    starter = next(
        (s.get("code", "") for s in snippets if s.get("langSlug") in language_config["slugs"]),
        "",
    )
    if not starter:
        raise RuntimeError(f"LeetCode did not provide a {language_config['name']} starter.")

    prompt = f"""
You are a careful LeetCode {language_config['name']} solver. Return ONLY a complete
{language_config['name']} solution file.
Use the exact LeetCode method/function signature from the starter snippet. Do not include
Markdown fences, prose, input parsing, or a main block. Prefer an efficient, correct
algorithm. Do not use external packages. Problem: {problem.get('title')}
ID: {problem.get('questionFrontendId')}
Difficulty: {problem.get('difficulty')}
Statement HTML:
{problem.get('content', '')}
Official {language_config['name']} starter:
{starter}
"""
    response = client.responses.create(
        model=MODEL,
        input=prompt,
        temperature=0.1,
    )
    code = response.output_text.strip()
    code = re.sub(r"^```(?:python|javascript|js)?\s*", "", code)
    code = re.sub(r"\s*```$", "", code)
    if language == "python":
        if "class Solution" not in code:
            raise RuntimeError("AI output did not contain a LeetCode Solution class.")
        compile(code, "<generated-solution>", "exec")
    elif not code:
        raise RuntimeError("AI output was empty.")
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


def test_and_maybe_submit(
    problem_id: str,
    slug: str,
    code: str,
    language: str,
) -> dict:
    extension = {"python": "py", "javascript": "js"}[language]
    solution_path = SOLUTIONS_DIR / f"{problem_id}.{slug}.{extension}"
    solution_path.write_text(code)
    test_result = run_command(["leetcode", "test", str(solution_path)], timeout=240)
    test_output = (test_result.stdout + "\n" + test_result.stderr).strip()
    (REPORTS_DIR / f"{problem_id}.{slug}.{language}.test.log").write_text(test_output + "\n")

    explicit_pass = bool(re.search(
        r"(all test cases passed|all tests passed|accepted|passed all test cases)",
        test_output, re.IGNORECASE
    ))
    explicit_fail = bool(re.search(
        r"(wrong answer|time limit exceeded|runtime error|compile error|failed|error:)",
        test_output, re.IGNORECASE
    ))
    result = {"language": language}
    if test_result.returncode != 0 or not explicit_pass or explicit_fail:
        result.update(status="TEST_FAILED_OR_UNCONFIRMED", details=test_output[-1200:])
        return result

    if DRY_RUN:
        result["status"] = "DRY_RUN_TEST_PASSED"
        return result

    submit_result = run_command(["leetcode", "submit", str(solution_path)], timeout=300)
    submit_output = (submit_result.stdout + "\n" + submit_result.stderr).strip()
    (REPORTS_DIR / f"{problem_id}.{slug}.{language}.submit.log").write_text(submit_output + "\n")
    if submit_result.returncode == 0 and re.search(r"\bAccepted\b", submit_output, re.IGNORECASE):
        result.update(status="SUBMITTED_CHECK_RESULT", details=submit_output[-1200:])
    else:
        result.update(status="SUBMIT_FAILED", details=submit_output[-1200:])
    return result


def process_problem(problem: dict, history: dict, report: list[dict]) -> None:
    problem_id = str(problem["questionFrontendId"])
    slug = problem["titleSlug"]
    title = problem["title"]
    url = f"https://leetcode.com/problems/{slug}/"
    item = {"id": problem_id, "title": title, "difficulty": problem["difficulty"], "url": url}
    print(f"Processing #{problem_id} {title} ({problem['difficulty']})")

    try:
        detail = fetch_detail(slug)
        SOLUTIONS_DIR.mkdir(parents=True, exist_ok=True)
        language_results = []
        for language in ("python", "javascript"):
            code = generate_solution(detail, language)
            language_results.append(test_and_maybe_submit(
                problem_id, slug, code, language,
            ))
        item["languages"] = language_results
        if any(result["status"] not in {"DRY_RUN_TEST_PASSED", "SUBMITTED_CHECK_RESULT"}
               for result in language_results):
            item["status"] = "LANGUAGE_FAILED"
            history.setdefault("attempted", []).append(problem_id)
            report.append(item)
            return

        if DRY_RUN:
            item["status"] = "DRY_RUN_TEST_PASSED"
            report.append(item)
            return

        item["status"] = "SUBMITTED_CHECK_RESULT"
        if all(result["status"] == "SUBMITTED_CHECK_RESULT" for result in language_results):
            history.setdefault("completed", []).append(problem_id)
        else:
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
