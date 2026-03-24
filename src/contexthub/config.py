from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://contexthub:contexthub@localhost:5432/contexthub"
    api_key: str = "changeme"
    embedding_model: str = "text-embedding-3-small"
    propagation_enabled: bool = True

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
