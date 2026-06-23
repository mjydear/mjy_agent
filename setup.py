"""Setuptools entrypoint for Athena Agent."""

from __future__ import annotations

from pathlib import Path

from setuptools import find_packages, setup


def read_requirements() -> list[str]:
    """Read runtime requirements from requirements.txt."""
    requirements_path = Path(__file__).parent / "requirements.txt"
    return [
        line.strip()
        for line in requirements_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


setup(
    name="athena-agent",
    version="0.1.0",
    description="Self-evolving enterprise-grade developer agent MVP.",
    packages=find_packages(exclude=("tests", "examples")),
    include_package_data=True,
    python_requires=">=3.11",
    install_requires=read_requirements(),
    entry_points={"console_scripts": ["athena=athena.cli.main:main"]},
)
