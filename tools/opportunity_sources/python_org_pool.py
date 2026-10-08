# tools/opportunity_sources/python_org_pool.py
"""
Gate 4.5-B1 Python 兼容组织种子池（支持热插拔扩展）。

设计意图：解耦"悬赏发现"与"语言确认"——
悬赏在 Algora 源发现，语言与活跃度由 GitHubEnricher 确认，
组织池只是发现入口的种子清单（projectdiscovery 为对照组）。
"""

PYTHON_ORG_POOL = [
    {"org": "mindsdb", "repo_hint": "mindsdb", "ecosystem": "python"},
    {"org": "pymc-labs", "repo_hint": "pymc", "ecosystem": "python"},
    {"org": "chaoss", "repo_hint": "augur", "ecosystem": "python"},
    {"org": "projectdiscovery", "repo_hint": "nuclei", "ecosystem": "go"},  # 对照组（非 Python）
]
