.PHONY: check dashboard ipk

check:
	./tests/check.sh
	python3 tests/check_secrets.py
	python3 -m unittest discover -s tests -p 'test_*.py'
	python3 -m unittest discover -s dashboard/tests -p 'test_*.py'

dashboard:
	python3 -X utf8 dashboard/server.py

ipk:
	python3 tools/build_ipk.py
