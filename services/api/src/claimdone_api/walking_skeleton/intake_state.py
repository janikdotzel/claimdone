"""Closed, redacted persisted state needed to rerun G0/G1 after restart."""

from fractions import Fraction

from pydantic import BaseModel, ConfigDict, Field

from claimdone_api.contracts.base import to_camel
from claimdone_api.media import (
    CaseHandle,
    ExifDecision,
    ImageFormat,
    IntakeSession,
    StoredAssetRef,
    StoredImage,
)


class PersistedModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        serialize_by_alias=True,
        validate_by_alias=True,
        validate_by_name=False,
    )


class PersistedAsset(PersistedModel):
    file_id: str
    media_type: str
    sha256: str

    @classmethod
    def from_ref(cls, value: StoredAssetRef) -> "PersistedAsset":
        return cls.model_validate(
            {
                "fileId": value.file_id,
                "mediaType": value.media_type,
                "sha256": value.sha256,
            }
        )

    def to_ref(self) -> StoredAssetRef:
        return StoredAssetRef(
            file_id=self.file_id,
            media_type=self.media_type,
            sha256=self.sha256,
        )


class PersistedImage(PersistedModel):
    input_id: str
    source: PersistedAsset
    image_format: ImageFormat


class PersistedIntake(PersistedModel):
    images: tuple[PersistedImage, ...] = Field(min_length=3, max_length=3)
    text: PersistedAsset | None
    audio: PersistedAsset | None
    statement: PersistedAsset
    exif_decisions: tuple[ExifDecision, ...] = Field(min_length=3, max_length=3)
    audio_duration_numerator: int | None
    audio_duration_denominator: int | None

    def to_session(self, handle: CaseHandle) -> IntakeSession:
        duration = None
        if self.audio_duration_numerator is not None:
            if self.audio_duration_denominator is None:
                raise ValueError("Persisted audio duration denominator is missing")
            duration = Fraction(
                self.audio_duration_numerator,
                self.audio_duration_denominator,
            )
        elif self.audio_duration_denominator is not None:
            raise ValueError("Persisted audio duration numerator is missing")
        return IntakeSession(
            handle=handle,
            images=tuple(
                StoredImage(
                    input_id=image.input_id,
                    source=image.source.to_ref(),
                    image_format=image.image_format,
                    exif_summary=(),
                )
                for image in self.images
            ),
            text=None if self.text is None else self.text.to_ref(),
            audio=None if self.audio is None else self.audio.to_ref(),
            audio_duration_seconds=duration,
        )


def persisted_intake(
    session: IntakeSession,
    *,
    statement: StoredAssetRef,
    exif_decisions: tuple[ExifDecision, ...],
) -> PersistedIntake:
    duration = session.audio_duration_seconds
    return PersistedIntake.model_validate(
        {
            "images": tuple(
                {
                    "inputId": image.input_id,
                    "source": PersistedAsset.from_ref(image.source),
                    "imageFormat": image.image_format,
                }
                for image in session.images
            ),
            "text": None if session.text is None else PersistedAsset.from_ref(session.text),
            "audio": None if session.audio is None else PersistedAsset.from_ref(session.audio),
            "statement": PersistedAsset.from_ref(statement),
            "exifDecisions": exif_decisions,
            "audioDurationNumerator": None if duration is None else duration.numerator,
            "audioDurationDenominator": None if duration is None else duration.denominator,
        }
    )
