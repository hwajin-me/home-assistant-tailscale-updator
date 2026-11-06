# Exercise this repository against the current Home Assistant Container release.
FROM ghcr.io/home-assistant/home-assistant:stable

WORKDIR /workspace
COPY . /workspace
RUN python -m pip install --no-cache-dir \
    'pytest>=8.3,<10' \
    'pytest-asyncio>=0.25,<2' \
    'ruff>=0.11,<1'

CMD ["/bin/sh", "-c", "ruff check . && ruff format --check . && python -m pytest tests/test_policy.py tests/test_container_compat.py tests/test_translations.py -q"]
