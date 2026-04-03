"""Verify the worktree_path fix in status.py"""
import ast
import sys

def check_file_syntax(path):
    """Check if a Python file has valid syntax."""
    with open(path, 'r') as f:
        content = f.read()
    try:
        ast.parse(content)
        print(f"✓ {path} - syntax OK")
        return True
    except SyntaxError as e:
        print(f"✗ {path} - syntax error: {e}")
        return False

def check_dag_tasks_schema(path):
    """Check if worktree_path is in dag_tasks schema."""
    with open(path, 'r') as f:
        content = f.read()
    if 'worktree_path TEXT' in content and '_CREATE_DAG_TASKS_TABLE_SQL' in content:
        print(f"✓ {path} - worktree_path in dag_tasks schema")
        return True
    else:
        print(f"✗ {path} - worktree_path missing from dag_tasks schema")
        return False

def check_upsert_dag_task(path):
    """Check if upsert_dag_task accepts worktree_path."""
    with open(path, 'r') as f:
        content = f.read()
    checks = [
        'worktree_path: str | None = None' in content,
        'worktree_path,' in content and 'worktree_path=CASE' in content,
        '"worktree_path": row[5]' in content or '"worktree_path": r[5]' in content,
    ]
    if all(checks):
        print(f"✓ {path} - upsert_dag_task has worktree_path")
        return True
    else:
        print(f"✗ {path} - upsert_dag_task missing worktree_path parameter or handling")
        return False

def check_status_query(path):
    """Check if status.py queries worktree_path directly from dag_tasks."""
    with open(path, 'r') as f:
        content = f.read()
    checks = [
        'dag_worktrees' in content,
        'dt.worktree_path' in content,
        'SELECT DISTINCT dt.worktree_path' in content,
        'FROM dag_tasks dt' in content,
        'known_worktrees.update(dag_worktrees)' in content,
    ]
    if all(checks):
        print(f"✓ {path} - status.py queries worktree_path from dag_tasks")
        return True
    else:
        print(f"✗ {path} - status.py doesn't properly query dag_tasks worktree_path")
        return False

def check_dispatch(path):
    """Check if dispatch code passes worktree_path."""
    with open(path, 'r') as f:
        content = f.read()
    if 'worktree_path=pane.worktree_path' in content:
        print(f"✓ {path} - dispatch passes worktree_path")
        return True
    else:
        print(f"✗ {path} - dispatch doesn't pass worktree_path")
        return False

def main():
    base = "/Users/jakegearon/projects/dgov/src/dgov"
    
    files_to_check = [
        f"{base}/persistence/schema.py",
        f"{base}/persistence/connection.py",
        f"{base}/persistence/dag_ops.py",
        f"{base}/status.py",
        f"{base}/dag_executor.py",
    ]
    
    all_ok = True
    
    # Check syntax for all files
    for path in files_to_check:
        if not check_file_syntax(path):
            all_ok = False
    
    # Check specific requirements
    if not check_dag_tasks_schema(f"{base}/persistence/schema.py"):
        all_ok = False
    
    if not check_upsert_dag_task(f"{base}/persistence/dag_ops.py"):
        all_ok = False
    
    if not check_status_query(f"{base}/status.py"):
        all_ok = False
    
    if not check_dispatch(f"{base}/dag_executor.py"):
        all_ok = False
    
    if all_ok:
        print("\n✓ All checks passed!")
        return 0
    else:
        print("\n✗ Some checks failed!")
        return 1

if __name__ == "__main__":
    sys.exit(main())
