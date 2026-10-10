"""Explicit source-only composites differ from missing dependency declarations."""

import pytest

from tools import validate_knowledge_base as validation


@pytest.mark.parametrize('mode,expected', [('source_only',None),('missing','requires depends_on'),('unpinned','dependency_versions keys'),('orphan_pin','dependency_versions keys')])
def test_composite_dependency_declaration(monkeypatch,mode,expected):
    original=validation.load_yaml
    def load(path):
        value=original(path)
        if path.name=='customer.display_name.yaml':
            value['depends_on']=[]
            value['dependency_versions']={}
            if mode=='missing':
                value.pop('depends_on')
            elif mode=='unpinned':
                value['depends_on']=['sales.outbound_actual']
            elif mode=='orphan_pin':
                value['dependency_versions']={'sales.outbound_actual':'2.0.0'}
        return value
    monkeypatch.setattr(validation,'load_yaml',load)
    errors,_,_=validation.validate_repository()
    relevant=[error for error in errors if 'customer.display_name.yaml' in error and ('depends_on' in error or 'dependency_versions keys' in error)]
    if expected is None:
        assert not relevant
    else:
        assert any(expected in error for error in relevant)
