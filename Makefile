# Architect: the few commands you run often. Everything is `python architect.py <cmd>` underneath (Windows: `py -3` works too).
PY ?= python

.PHONY: eval eval-quick eval-fake eval-baseline eval-compare selftest selftest-all selftest-full ready status dist

eval:            ## every task in evals/ through the real flows with your models; results in hub_data/evals/
	$(PY) architect.py eval --label live

eval-quick:      ## the first 10 tasks per flow with your models
	$(PY) architect.py eval --quick --label quick

eval-fake:       ## the whole set against the scripted oracle: proves the harness, needs no model or GPU
	$(PY) architect.py eval --fake --label fake

eval-baseline:   ## record the baseline the regression gate compares against (run after a change you trust)
	$(PY) architect.py eval --quick --label baseline

eval-compare:    ## quick eval now vs the latest 'baseline'; exit 1 on a regression
	$(PY) architect.py eval --quick --label candidate --compare baseline

selftest:        ## engine, flows, tune, eval harness, offline seal (no model)
	$(PY) architect.py selftest --suite core

selftest-all:    ## + pro/security/hub suites
	$(PY) architect.py selftest --suite all

selftest-full:   ## + the self-improvement pipeline
	$(PY) architect.py selftest --suite full

ready:           ## first run: repair, model, tune, prove, smoke test
	$(PY) architect.py ready

status:
	$(PY) architect.py status

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /  /'

dist:            ## an offline installer for this machine's hardware profile (PROFILE=gpu6 to choose; WITH=--with-server to include the model server)
	$(PY) architect.py dist $(if $(PROFILE),--profile $(PROFILE),) $(WITH) --zip
