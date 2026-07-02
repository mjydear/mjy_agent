"""
HashiCorp Vault integration for secret management.

Provides a VaultClient for reading secrets at startup, with automatic
fallback to environment variables when Vault is unavailable.

Usage:
    from athena.config.vault import VaultClient, VaultSecretRef

    # Direct access
    client = VaultClient(url="https://vault.example.com", token_env="VAULT_TOKEN")
    secret = await client.read("secret/athena/openai-api-key")

    # YAML reference (used in config files)
    # api_key: !vault secret/athena/openai-api-key#key
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


class SecretSource(Protocol):
    """Protocol for any secret provider (Vault, env, AWS Secrets Manager, etc.)."""

    async def read(self, path: str, key: str) -> str | None:
        """Read a secret value by path and key."""


@dataclass
class VaultSecretRef:
    """Reference to a Vault secret, usable in YAML config files.

    Example YAML:
        api_key: !vault secret/athena/openai-api-key#api_key
    """

    path: str
    key: str

    @classmethod
    def parse(cls, ref: str) -> VaultSecretRef:
        """Parse a vault reference string like 'secret/path#key'."""
        if "#" not in ref:
            raise ValueError(
                f"Invalid Vault reference: '{ref}'. Expected format: 'secret/path#key'"
            )
        path, key = ref.rsplit("#", 1)
        if not path or not key:
            raise ValueError(
                f"Invalid Vault reference: '{ref}'. Both path and key are required."
            )
        return cls(path=path, key=key)


class EnvFallbackSource:
    """Secret source that reads from environment variables (always available)."""

    async def read(self, path: str, key: str) -> str | None:
        """Read from environment variable. Path is ignored, key maps to env var name."""
        return os.getenv(key)


class VaultClient:
    """HashiCorp Vault secret reader with graceful degradation.

    When Vault is unavailable, automatically falls back to environment variables
    via EnvFallbackSource.

    Usage:
        client = VaultClient(
            url="https://vault.example.com:8200",
            token_env="VAULT_TOKEN",
        )
        # If Vault is reachable, reads from Vault.
        # If not, falls back to OPENAI_API_KEY env var.
        api_key = await client.read("secret/athena/openai", "api_key")
    """

    def __init__(
        self,
        url: str | None = None,
        token_env: str = "VAULT_TOKEN",
        timeout: float = 10.0,
        enabled: bool = True,
    ) -> None:
        """Initialize Vault client.

        Args:
            url: Vault server URL. If None, reads VAULT_ADDR env var.
            token_env: Environment variable name for the Vault token.
            timeout: Request timeout in seconds.
            enabled: Whether Vault is enabled. Set to False to skip Vault entirely.
        """
        self._url = url or os.getenv("VAULT_ADDR", "http://localhost:8200")
        self._token_env = token_env
        self._timeout = timeout
        self._enabled = enabled
        self._fallback = EnvFallbackSource()
        self._healthy: bool | None = None  # None = not checked yet
        self._http_client: httpx.AsyncClient | None = None

    @property
    def is_healthy(self) -> bool | None:
        """Return Vault health status. None means not checked yet."""
        return self._healthy

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                verify=True,
            )
        return self._http_client

    async def check_health(self) -> bool:
        """Check if Vault is reachable and healthy.

        Returns:
            True if Vault is healthy, False otherwise.
        """
        if not self._enabled:
            self._healthy = False
            return False

        try:
            client = await self._get_client()
            response = await client.get(f"{self._url}/v1/sys/health")
            self._healthy = response.status_code == 200
        except Exception:
            self._healthy = False
            logger.debug("Vault health check failed at %s", self._url)

        return self._healthy

    async def read(self, path: str, key: str) -> str | None:
        """Read a secret from Vault, falling back to env var if unavailable.

        Args:
            path: Vault secret path, e.g. 'secret/athena/openai'.
            key: Key within the secret, e.g. 'api_key'.

        Returns:
            The secret value, or None if not found.
        """
        # Try Vault
        if self._enabled:
            if self._healthy is None:
                await self.check_health()

            if self._healthy:
                try:
                    value = await self._read_from_vault(path, key)
                    if value is not None:
                        return value
                except Exception as exc:
                    logger.warning(
                        "Vault read failed for %s#%s: %s. Falling back to env.",
                        path,
                        key,
                        exc,
                    )

        # Fallback to environment variable
        return await self._fallback.read(path, key)

    async def _read_from_vault(self, path: str, key: str) -> str | None:
        """Read a single key from a Vault KV v2 secret."""
        token = os.getenv(self._token_env)
        if not token:
            logger.debug("Vault token not set (env: %s)", self._token_env)
            return None

        client = await self._get_client()
        headers = {"X-Vault-Token": token}
        response = await client.get(
            f"{self._url}/v1/{path}",
            headers=headers,
        )

        if response.status_code == 404:
            return None
        response.raise_for_status()

        data = response.json()
        # KV v2 format: {"data": {"data": {...}}}
        secret_data = data.get("data", {}).get("data", {})
        return secret_data.get(key)

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None


async def resolve_vault_refs(
    data: dict[str, object],
    vault: VaultClient | None = None,
) -> dict[str, object]:
    """Recursively resolve VaultSecretRef values in a config dict.

    Scans a nested dict for VaultSecretRef instances and replaces them
    with actual secret values from Vault (or env fallback).

    Args:
        data: Configuration dict potentially containing VaultSecretRef values.
        vault: VaultClient instance. Created with default settings if None.

    Returns:
        A new dict with all VaultSecretRef values resolved.
    """
    if vault is None:
        vault = VaultClient()

    result: dict[str, object] = {}
    for key, value in data.items():
        if isinstance(value, VaultSecretRef):
            secret = await vault.read(value.path, value.key)
            if secret is None:
                raise ValueError(
                    f"Failed to resolve Vault reference: {value.path}#{value.key}"
                )
            result[key] = secret
        elif isinstance(value, dict):
            result[key] = await resolve_vault_refs(value, vault)  # type: ignore[arg-type]
        elif isinstance(value, list):
            result[key] = [
                await _resolve_item(item, vault) for item in value
            ]
        else:
            result[key] = value
    return result


async def _resolve_item(
    item: object, vault: VaultClient
) -> object:
    """Resolve a single item in a list (could be VaultSecretRef or dict)."""
    if isinstance(item, VaultSecretRef):
        return await vault.read(item.path, item.key)
    if isinstance(item, dict):
        return await resolve_vault_refs(item, vault)  # type: ignore[arg-type]
    return item