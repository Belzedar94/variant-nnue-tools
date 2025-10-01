# -*- coding: utf-8 -*-

from setuptools import setup, Extension
from glob import glob
import platform
import io
import os


if platform.python_compiler().startswith("MSC"):
    args = ["/std:c++17"]
else:
    args = ["-std=c++17", "-flto", "-Wno-date-time"]

macros = [
    ("LARGEBOARDS", None),
    ("ALLVARS", None),
    ("PRECOMPUTED_MAGICS", None),
    ("NNUE_EMBEDDING_OFF", None),
]

# Mirror the default Makefile NNUE data size so headers that rely on the
# DATA_SIZE macro (for packed SFEN support) compile correctly when building the
# Python extension outside of the engine Makefile.
data_size = os.environ.get("PYFFISH_DATA_SIZE", "512")
macros.append(("DATA_SIZE", data_size))

if "64bit" in platform.architecture():
    macros.append(("IS_64BIT", None))

CLASSIFIERS = [
    "Development Status :: 3 - Alpha",
    "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
    "Programming Language :: Python :: 3",
    "Operating System :: OS Independent",
]

with io.open("README.md", "r", encoding="utf8") as fh:
    long_description = fh.read().strip()

sources = glob("src/*.cpp") + glob("src/syzygy/*.cpp") + glob("src/nnue/*.cpp") + glob("src/nnue/features/*.cpp")
headers = glob("src/*.h") + glob("src/syzygy/*.h") + glob("src/nnue/*.h") + glob("src/nnue/features/*.h")
ffish_source_file = os.path.normcase("src/ffishjs.cpp")
try:
    sources.remove(ffish_source_file)
except ValueError:
    print(f"ffish_source_file {ffish_source_file} was not found in sources {sources}.")

pyffish_module = Extension(
    "pyffish",
    sources=sources,
    depends=headers,
    include_dirs=["src"],
    extra_compile_args=args,
    define_macros=macros)

setup(name="pyffish", version="0.0.88",
      description="Fairy-Stockfish Python wrapper",
      long_description=long_description,
      long_description_content_type="text/markdown",
      author="Bajusz Tamás",
      author_email="gbtami@gmail.com",
      license="GPL3",
      classifiers=CLASSIFIERS,
      url="https://github.com/gbtami/Fairy-Stockfish",
      python_requires=">=2.7,!=3.0.*,!=3.1.*,!=3.2.*,!=3.3.*",
      ext_modules=[pyffish_module],
      data_files=[("", ["pyffish.pyi"])]
      )
