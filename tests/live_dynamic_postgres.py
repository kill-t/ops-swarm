"""为现场动态协作验收显式命名本项目临时PostgreSQL。"""

import pytest

from tests import conftest


@pytest.fixture(scope="session")
def live_dynamic_postgres_config():
    original = conftest.PostgresContainer

    def container(*args, **kwargs):
        return (
            original(*args, **kwargs)
            .with_name("opsswarm-live-dynamic-postgres")
            .with_command(["postgres", "-c", "jit=off"])
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(conftest, "PostgresContainer", container)
        yield


@pytest.fixture(scope="session")
def migrated_database_url(live_dynamic_postgres_config, migrated_database_url):
    return migrated_database_url
