PYTHON ?= python3
export PYTHONPATH := src

.PHONY: test serve check

test:
	$(PYTHON) -m unittest discover -s tests -v

serve:
	$(PYTHON) -m dochub serve

check:
	$(PYTHON) -m dochub check
