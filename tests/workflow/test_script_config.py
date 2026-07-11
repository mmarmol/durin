def test_workflow_script_defaults():
    from durin.config.schema import WorkflowConfig
    cfg = WorkflowConfig()
    assert cfg.script_timeout == 300
    assert cfg.script_output_max_chars == 16000
