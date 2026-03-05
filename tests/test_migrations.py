from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def test_alembic_upgrade_creates_phase1_tables(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "alembic_phase1.db"
    database_url = f"sqlite:///{db_path}"

    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(alembic_cfg, "head")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    expected_tables = {
        "operator",
        "conversation_session",
        "ad_draft",
        "published_ad",
        "system_config",
        "audit_event",
        "processed_inbound_message",
    }
    assert expected_tables.issubset(tables)

    ad_draft_columns = {column["name"] for column in inspector.get_columns("ad_draft")}
    assert "enrichment_source" in ad_draft_columns
    assert "product_brand" in ad_draft_columns

    operator_columns = {column["name"] for column in inspector.get_columns("operator")}
    assert {"business_name", "logo_url", "brand_colors"}.issubset(operator_columns)

    conversation_columns = {
        column["name"] for column in inspector.get_columns("conversation_session")
    }
    assert "pending_upload_type" in conversation_columns
