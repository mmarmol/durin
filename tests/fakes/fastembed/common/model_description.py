"""The two names durin imports from fastembed.common.model_description to
register a custom model; the stand-in TextEmbedding reads neither."""


class PoolingType:
    MEAN = "MEAN"
    CLS = "CLS"


class ModelSource:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
