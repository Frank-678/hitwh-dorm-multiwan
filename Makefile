.PHONY: check dashboard

check:
	./tests/check.sh
	python3 -m unittest discover -s dashboard/tests -p 'test_*.py'

dashboard:
	python3 -X utf8 dashboard/server.py
