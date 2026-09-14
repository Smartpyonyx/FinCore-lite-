# FinCore Lite v0.1

The accounting tool for every person, every business, every African market.

## Quick Start

### Prerequisites
- Python 3.11+
- PostgreSQL 15+
- Redis 7+

### Installation

```bash
# Clone the repository
cd fincore-lite-v0.1/backend

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy environment template and configure
cp .env.example .env
# Edit .env with your settings (IMPORTANT: set SECRET_KEY!)

# Run database migrations
alembic upgrade head

# Start development server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### API Documentation
- Swagger UI: http://localhost:8000/api/docs
- ReDoc: http://localhost:8000/api/redoc

### Configuration

All configuration is done via environment variables in `.env`. See `.env.example` for all available options.

**Critical settings that MUST be changed for production:**
- `SECRET_KEY` - Generate with: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- `DATABASE_URL` - Your PostgreSQL connection string
- `MPESA_CONSUMER_KEY` / `MPESA_CONSUMER_SECRET` - From Safaricom Developer Portal
- `EXCHANGE_RATE_API_KEY` - From Open Exchange Rates

### Project Structure

```
backend/
├── app/
│   ├── core/           # Configuration, security, database
│   ├── models/         # SQLAlchemy models
│   ├── routers/        # API route handlers
│   ├── schemas/        # Pydantic schemas
│   └── main.py         # FastAPI application
├── requirements.txt
├── .env.example
└── alembic/            # Database migrations
```

### Features
- **Double-entry accounting** with chart of accounts
- **M-Pesa Daraja integration** (STK Push, Callbacks, B2C)
- **Multi-currency support** with exchange rates
- **MFA/TOTP** for enhanced security
- **Audit logging** for compliance
- **Financial reports** (P&L, Balance Sheet, Cash Flow)
- **Role-based access control** (Owner, Accountant, Staff, Viewer)

### Testing

```bash
pytest tests/
```

### Deployment

For production, use:
- Gunicorn with Uvicorn workers
- PostgreSQL with connection pooling (PgBouncer)
- Redis for caching and rate limiting
- Reverse proxy (Nginx) with SSL
- Docker/Kubernetes for orchestration

```bash
# Production server
gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```