#!/bin/sh
# Pre-commit hook: block any commit that would publish personal data. See tools/leak_gate.py.
# Install: ln -s ../../scripts/pre-commit-leak-gate.sh .git/hooks/pre-commit
exec python3 "$(git rev-parse --show-toplevel)/tools/leak_gate.py" --staged
