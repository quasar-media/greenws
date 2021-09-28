SOURCES := $(shell find -name '*.rst')

SPHINXFLAGS =

_build: $(SOURCES)
	sphinx-build $(SPHINXFLAGS) . $@ $?

.PHONY: clean
clean:
	rm -rf _build
