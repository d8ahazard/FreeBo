# eboproto - portable EBO control-protocol codec. Zero external deps.
#
#   make            # build static lib + self-test
#   make test       # build and run the self-test
#   make shared     # build a shared library (.so/.dylib/.dll)
#   make clean

CC      ?= cc
AR      ?= ar
CFLAGS  ?= -std=c99 -O2 -Wall -Wextra -Wpedantic
PREFIX  ?= /usr/local

OBJ      = eboproto.o
LIB      = libeboproto.a
SELFTEST = eboproto_selftest

.PHONY: all test shared clean install

all: $(LIB) $(SELFTEST)

$(OBJ): eboproto.c eboproto.h
	$(CC) $(CFLAGS) -c eboproto.c -o $(OBJ)

$(LIB): $(OBJ)
	$(AR) rcs $(LIB) $(OBJ)

$(SELFTEST): eboproto.c eboproto_selftest.c eboproto.h
	$(CC) $(CFLAGS) eboproto.c eboproto_selftest.c -o $(SELFTEST)

test: $(SELFTEST)
	./$(SELFTEST)

# Shared library. On Windows/MinGW this produces eboproto.dll.
shared: eboproto.c eboproto.h
	$(CC) $(CFLAGS) -fPIC -shared eboproto.c -o libeboproto.so

install: $(LIB)
	install -d $(PREFIX)/lib $(PREFIX)/include
	install -m644 $(LIB) $(PREFIX)/lib/
	install -m644 eboproto.h $(PREFIX)/include/

clean:
	rm -f $(OBJ) $(LIB) $(SELFTEST) libeboproto.so eboproto.dll
