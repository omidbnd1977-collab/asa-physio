.PHONY: help install dev test lint fmt build migrate serve stop deploy rollback backup restore loadtest seed
help:            ## show this help
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n",$$1,$$2}'
install:         ## install deps
	pip install -r requirements.txt -r requirements-dev.txt
test:            ## run the full test suite
	python3 -m pytest -q
lint:            ## lint + format check
	ruff check api tests build_static.py && ruff format --check api tests build_static.py
fmt:             ## autoformat
	ruff format api tests build_static.py && ruff check --fix api tests build_static.py
build:           ## rebuild the site, the public/ bundle and the offline demo
	python3 work/build_pages.py && python3 build_static.py && python3 work/build_demo.py
demo:            ## rebuild only the offline booking demo
	python3 work/build_demo.py
migrate:         ## apply pending migrations
	python3 -m api.db
serve:           ## run locally
	ops/serve.sh restart staging
stop:            ## stop the local server
	ops/serve.sh stop staging
deploy:          ## one-command deploy (ENV=staging|production)
	ops/deploy.sh $(or $(ENV),staging)
rollback:        ## roll back to the previous release
	ops/rollback.sh
backup:          ## take a verified backup
	ops/backup.sh
loadtest:        ## measure the bottleneck
	python3 ops/loadtest.py
seed:            ## create the first admin user
	python3 -m ops.seed
