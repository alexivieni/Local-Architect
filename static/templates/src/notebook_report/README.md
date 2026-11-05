# __NAME__

Write an analysis as a plain Python file split into `# %%` cells (the same markers VS Code and Jupytext use).
The runner executes the cells in order in one namespace, captures what each prints, and builds a report.

    python -m __PKG__ analysis.py --out report.html            # HTML report
    python -m __PKG__ analysis.py --ipynb analysis.ipynb       # also a notebook with outputs
    python -m pytest -q

Inside a cell, `show(obj)` adds rich output: a list of dicts becomes a table, `bar({...})` an SVG bar chart.
Markdown cells start with `# %% [markdown]`; their comment lines become paragraphs and headings.

A cell that raises is recorded with its traceback and the run stops there (later cells depend on earlier ones).
