ENGINE_DIR ?= engine/Atomic-Stockfish
ENGINE_SRC := $(ENGINE_DIR)/src
ARCH ?= x86-64
COMP ?= gcc
debug ?= no
optimize ?= yes
sanitize ?= none
EXTRACXXFLAGS ?=
ifeq ($(OS),Windows_NT)
PYTHON ?= python
else
PYTHON ?= python3
endif

ifeq ($(COMP),mingw)
TOOLS_EXE := atomic-data-tools.exe
PLAYING_ENGINE_EXE := atomic-stockfish.exe
DATA_GENERATOR_EXE := atomic-stockfish-data-generator.exe
TOOLS_UNIT_CXX ?= x86_64-w64-mingw32-c++
else
TOOLS_EXE := atomic-data-tools
PLAYING_ENGINE_EXE := atomic-stockfish
DATA_GENERATOR_EXE := atomic-stockfish-data-generator
ifeq ($(COMP),clang)
TOOLS_UNIT_CXX ?= clang++
else
TOOLS_UNIT_CXX ?= g++
endif
endif

.PHONY: help schema-header schema-header-check verify-engine-pin pin-tests verify-delegated-tsan data-tools \
	tools-unit tools-integration wrapper-integration playing-engine \
	data-generator data-generator-tests engine-legacy-unit test clean

help:
	@echo "Atomic NNUE tools targets:"
	@echo "  schema-header      regenerate the tools handshake from the pinned schema"
	@echo "  schema-header-check  reject a stale generated handshake header"
	@echo "  verify-engine-pin  verify the exact Atomic-Stockfish gitlink and schema"
	@echo "  data-tools         build the temporary Legacy V1 validate/convert/stats backend"
	@echo "  playing-engine     build the pinned Atomic-Stockfish playing binary"
	@echo "  data-generator     build the pinned Atomic-Stockfish data generator"
	@echo "  data-generator-tests  run the pinned generator fixture suite"
	@echo "  engine-legacy-unit    run the pinned Legacy Atomic V1 C++ codec unit"
	@echo "  verify-delegated-tsan verify the pinned Threads=2 TSan generator gate"
	@echo "  test                  run pin, codec and tools-backend tests"

schema-header:
	$(PYTHON) script/generate_atomic_schema_header.py

schema-header-check:
	$(PYTHON) script/generate_atomic_schema_header.py --check

verify-engine-pin: schema-header-check
	$(PYTHON) tests/atomic_engine_pin.py

pin-tests:
	$(PYTHON) tests/test_atomic_engine_pin.py

verify-delegated-tsan: verify-engine-pin
	$(PYTHON) tests/atomic_tsan_delegation.py

data-tools: schema-header-check
	+$(MAKE) -C src EXE=$(TOOLS_EXE) clean
	+$(MAKE) -C src ARCH=$(ARCH) COMP=$(COMP) debug=$(debug) \
		optimize=$(optimize) sanitize="$(sanitize)" \
		EXTRACXXFLAGS="$(strip $(EXTRACXXFLAGS) -DATOMIC_DATA_TOOLS)" \
		EXE=$(TOOLS_EXE) build

tools-unit: schema-header-check
	CXX="$(TOOLS_UNIT_CXX)" bash tests/tools_unit.sh

tools-integration: data-tools
	$(PYTHON) tests/tools_integration.py --engine src/$(TOOLS_EXE)

playing-engine: verify-engine-pin
	+$(MAKE) -C $(ENGINE_SRC) ARCH=$(ARCH) COMP=$(COMP) debug=$(debug) \
		optimize=$(optimize) sanitize="$(sanitize)" \
		EXTRACXXFLAGS="$(strip $(EXTRACXXFLAGS))" build
	@test -f "$(ENGINE_SRC)/$(PLAYING_ENGINE_EXE)"

data-generator: verify-engine-pin
	+$(MAKE) -C $(ENGINE_SRC) ARCH=$(ARCH) COMP=$(COMP) debug=$(debug) \
		optimize=$(optimize) sanitize="$(sanitize)" \
		EXTRACXXFLAGS="$(strip $(EXTRACXXFLAGS))" data-generator
	@test -f "$(ENGINE_SRC)/$(DATA_GENERATOR_EXE)"

data-generator-tests: verify-engine-pin
	+$(MAKE) -C $(ENGINE_SRC) ARCH=$(ARCH) COMP=$(COMP) \
		debug=$(debug) optimize=$(optimize) sanitize="$(sanitize)" \
		EXTRACXXFLAGS="$(strip $(EXTRACXXFLAGS))" \
		ATOMIC_NNUE_TEST_NET="$(ATOMIC_NNUE_TEST_NET)" data-generator-tests

engine-legacy-unit: verify-engine-pin
	+$(MAKE) -C $(ENGINE_SRC) ARCH=$(ARCH) COMP=$(COMP) debug=$(debug) \
		optimize=$(optimize) sanitize="$(sanitize)" \
		EXTRACXXFLAGS="$(strip $(EXTRACXXFLAGS))" legacy-atomic-v1-tests

wrapper-integration: data-tools data-generator
	$(PYTHON) tests/atomic_wrapper_integration.py \
		--generator "$(ENGINE_SRC)/$(DATA_GENERATOR_EXE)" \
		--tools "src/$(TOOLS_EXE)" \
		--net "$(ATOMIC_NNUE_TEST_NET)"

test: verify-engine-pin pin-tests verify-delegated-tsan tools-unit tools-integration

clean:
	$(MAKE) -C src EXE=$(TOOLS_EXE) clean
	$(MAKE) -C $(ENGINE_SRC) clean
