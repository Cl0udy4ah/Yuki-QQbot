"""Small cursor state stored in the plugin's private KV."""

from __future__ import annotations

from .models import RepositoryState

NAMESPACE = "github_monitor"


async def load_repository_state(context: object, repository: str) -> RepositoryState:
    value = await context.storage.get(NAMESPACE, repository.casefold())
    return RepositoryState.model_validate(value or {})


async def save_repository_state(
    context: object,
    repository: str,
    state: RepositoryState,
) -> None:
    await context.storage.set(
        NAMESPACE,
        repository.casefold(),
        state.model_dump(mode="json"),
    )


async def delete_repository_state(context: object, repository: str) -> None:
    await context.storage.delete(NAMESPACE, repository.casefold())
