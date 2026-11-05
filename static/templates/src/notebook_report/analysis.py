# %% [markdown]
# # Coffee sales
# A tiny example analysis. Each cell runs in order; outputs land in the report.

# %%
sales = [
    {"day": "Mon", "cups": 42}, {"day": "Tue", "cups": 51}, {"day": "Wed", "cups": 38},
    {"day": "Thu", "cups": 60}, {"day": "Fri", "cups": 74},
]
print(f"{len(sales)} days, {sum(r['cups'] for r in sales)} cups")

# %%
show(sales)

# %% [markdown]
# ## Cups per day

# %%
show(bar({r["day"]: r["cups"] for r in sales}))
best = max(sales, key=lambda r: r["cups"])
print(f"best day: {best['day']} ({best['cups']})")
