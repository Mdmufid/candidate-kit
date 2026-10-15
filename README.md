# Employee Attendance & Analytics API

## How to run
1. Ensure MongoDB 6.0+ is running.
2. Set the `MONGO_URI` and `MONGO_DB` environment variables (a local `.env` file works for development).
3. Install dependencies: `pip install -r requirements.txt`
4. Start the server: `uvicorn app.main:app --port 8000`

## Notes
* All endpoints, including the complex analytics pipelines and the `explain` route, are implemented using MongoDB native aggregations to ensure performance at scale.
* Concurrency issues are handled natively via database unique indexes.