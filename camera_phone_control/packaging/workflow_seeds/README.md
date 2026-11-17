# Curated installer workflow seeds

Only manually approved production workflow JSON files belong in this directory.
The PyInstaller build deliberately does not read `automation/workflow_data`, which
is the live development/runtime workflow store.

Before adding a workflow:

- remove all `recording_screenshot` data;
- remove personal/test bank, account and amount defaults;
- confirm that it contains no receipt, credential, job or journal data;
- run `python packaging\validate_workflow_seeds.py packaging\workflow_seeds`.

An empty directory is valid. In that case the client commissions or imports its
workflows after installation rather than receiving an unsafe sample workflow.
