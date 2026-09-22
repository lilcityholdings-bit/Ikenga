"""
A headless Streamlit.

The real package will not install in this sandbox, so the dashboard was only
ever compile-checked. Compiling proves the syntax is legal; it proves nothing
about whether the page actually runs. This stub implements the slice of the
Streamlit API the dashboard uses, records every call, and lets a test click
any button by name.
"""
import sys
import types
from contextlib import contextmanager


class Rerun(Exception):
    """Raised by st.rerun(), same control-flow role as the real one."""


class Stop(Exception):
    """Raised by st.stop()."""


class Recorder:
    def __init__(self):
        self.calls = []
        self.text = []
        self.errors = []
        self.metrics = {}
        self.click = None          # label of the button that should return True
        self.clicked = []
        self.inputs = {}           # override widget return values by label

    def log(self, kind, *args):
        self.calls.append((kind, args))


REC = Recorder()


class _Element:
    """Columns and containers expose the same widget API as the module."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, name):
        return getattr(sys.modules["streamlit"], name)


def _make():
    st = types.ModuleType("streamlit")

    # ---- layout / output
    def set_page_config(**kw):
        REC.log("set_page_config", kw)
    st.set_page_config = set_page_config

    def _text(kind):
        def fn(*a, **kw):
            body = " ".join(str(x) for x in a)
            REC.log(kind, body)
            REC.text.append(f"[{kind}] {body}")
            if kind == "error":
                REC.errors.append(body)
        return fn

    for name in ("title", "header", "subheader", "write", "caption", "markdown",
                 "success", "warning", "error", "info", "code", "json", "divider"):
        setattr(st, name, _text(name))

    def metric(label, value, *a, **kw):
        REC.metrics[label] = value
        REC.log("metric", label, value)
    st.metric = metric

    def columns(spec, **kw):
        n = spec if isinstance(spec, int) else len(spec)
        return [_Element() for _ in range(n)]
    st.columns = columns

    @contextmanager
    def expander(label, **kw):
        REC.log("expander", label)
        yield _Element()
    st.expander = expander

    @contextmanager
    def container(**kw):
        yield _Element()
    st.container = container

    @contextmanager
    def spinner(label="", **kw):
        yield
    st.spinner = spinner

    # ---- widgets
    def button(label, key=None, **kw):
        hit = (REC.click is not None and (REC.click == label or REC.click == key))
        if hit:
            REC.clicked.append(label)
        REC.log("button", label, key)
        return hit
    st.button = button

    def toggle(label, value=False, **kw):
        return REC.inputs.get(label, value)
    st.toggle = toggle

    def text_input(label, value="", **kw):
        return REC.inputs.get(label, value)
    st.text_input = text_input

    def text_area(label, value="", **kw):
        return REC.inputs.get(label, value)
    st.text_area = text_area

    def number_input(label, *a, **kw):
        if label in REC.inputs:
            return REC.inputs[label]
        if "value" in kw:
            return kw["value"]
        return a[2] if len(a) > 2 else (a[0] if a else 0)
    st.number_input = number_input

    def selectbox(label, options, format_func=None, **kw):
        opts = list(options)
        if label in REC.inputs:
            return REC.inputs[label]
        return opts[0] if opts else None
    st.selectbox = selectbox

    # ---- state / control
    st.session_state = {"authed": True}

    def rerun():
        raise Rerun()
    st.rerun = rerun

    def stop():
        raise Stop()
    st.stop = stop

    class _Cache:
        def __call__(self, *a, **kw):
            def deco(fn):
                return fn
            if a and callable(a[0]):
                return a[0]
            return deco

        def clear(self):
            REC.log("cache_clear")
    st.cache_data = _Cache()

    return st


def install():
    sys.modules["streamlit"] = _make()
    return REC


def reset(click=None, inputs=None):
    REC.calls.clear()
    REC.text.clear()
    REC.errors.clear()
    REC.metrics.clear()
    REC.clicked.clear()
    REC.click = click
    REC.inputs = inputs or {}
    sys.modules["streamlit"].session_state = {"authed": True}
    return REC
