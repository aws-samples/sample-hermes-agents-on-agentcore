#!/bin/sh
set -eu
HERMES_COMMIT=debfc7420b61a96ad97fc03b18cca74d7e72697d
git init /opt/hermes
git -C /opt/hermes remote add origin https://github.com/NousResearch/hermes-agent.git
git -C /opt/hermes fetch --depth 1 origin "$HERMES_COMMIT"
git -C /opt/hermes checkout FETCH_HEAD
python -m venv /opt/venv
/opt/venv/bin/pip install --no-cache-dir -e '/opt/hermes[anthropic]' boto3==1.43.97
rm -rf /opt/hermes/.git
