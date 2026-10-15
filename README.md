# Employee Attendance & Analytics API

A FastAPI and MongoDB REST API for employee attendance, manual
corrections, and workforce analytics, built for the HROne engineering
assignment.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+.

1.  Install dependencies:

    ``` bash
    pip install -r requirements.txt
    ```

2.  Set `MONGO_URI` and `MONGO_DB` as environment variables (or in a
    local, uncommitted `.env` file).

3.  Start the server from the repository root:

    ``` bash
    uvicorn app.main:app --reload --port 8000
    ```

4.  Open Swagger UI at http://localhost:8000/docs. Check database
    readiness at http://localhost:8000/health.

## Endpoints

-   `GET /health` --- database readiness check.
-   `POST /employees`, `GET /employees` --- create and list employees.
-   `POST /attendance/punch-in`, `POST /attendance/punch-out` --- record
    attendance.
-   `GET /attendance` --- filter and paginate attendance records.
-   `PATCH /attendance/{emp_code}/{date}` --- regularize a record with
    an audit history.
-   `GET /analytics/employees/{emp_code}/monthly` --- employee monthly
    summary.
-   `GET /analytics/departments/summary` --- department monthly summary.
-   `GET /analytics/leaderboard/late` --- late-arrival leaderboard.
-   `GET /analytics/departments/{department}/trend` --- daily department
    trend.
-   `GET /admin/explain/{endpoint}` --- inspect MongoDB query plans.

## Notes

-   Timestamps use epoch milliseconds; attendance dates and shift times
    follow IST.
-   MongoDB unique indexes protect employee codes and daily attendance
    records.
-   Analytics use MongoDB aggregation pipelines; pagination is one-based
    with a maximum page size of 100.
-   See `openapi.yaml` for the full API contract, `REVIEW.md` for
    code-review findings, and `DECISIONS.md` for implementation
    decisions.

## Submit

The public repository should contain `app/main.py`, `requirements.txt`,
`REVIEW.md`, `DECISIONS.md`, and this short `README.md`. Do not commit
`.env`, secrets, or a Dockerfile.
