# Repository Guidance

- This app uses FastAPI, SQLAlchemy, SQLite by default, and a background market worker. Keep the worker independent of browser sessions.
- All orders must remain simulated. Never add broker credentials or live order execution without an explicit product and security review.
- Create database tables at startup and keep models portable between SQLite and PostgreSQL.
- Use the existing pytest suite for strategy, persistence, API, and worker behavior.
- The market-data provider may delay or deny requests. Do not invent prices or place trades when valid market data is unavailable.
