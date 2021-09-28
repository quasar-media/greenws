from greenws import __version__

project = "greenws"
copyright = "2021, Auri"
author = "Auri"
release = __version__

html_theme = "alabaster"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
]

autodoc_member_order = "bysource"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3/", None),
    "gevent": ("http://www.gevent.org/", None),
}

exclude_patterns = ["_build"]
