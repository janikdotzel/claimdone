import { MAX_AUDIO_SECONDS } from "./types";

export function validateAudioDuration(durationSeconds: number) {
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    return "The audio duration could not be read.";
  }
  if (durationSeconds > MAX_AUDIO_SECONDS) {
    return "Audio must be 60 seconds or less.";
  }
  return null;
}

export function inspectAudioDuration(previewUrl: string): Promise<number> {
  return new Promise((resolve, reject) => {
    const audio = document.createElement("audio");
    const cleanup = () => {
      window.clearTimeout(timeout);
      audio.removeEventListener("loadedmetadata", handleLoadedMetadata);
      audio.removeEventListener("error", handleError);
      audio.removeAttribute("src");
    };
    const handleLoadedMetadata = () => {
      const duration = audio.duration;
      cleanup();
      resolve(duration);
    };
    const handleError = () => {
      cleanup();
      reject(new Error("The audio duration could not be read."));
    };

    audio.preload = "metadata";
    audio.addEventListener("loadedmetadata", handleLoadedMetadata, { once: true });
    audio.addEventListener("error", handleError, { once: true });
    const timeout = window.setTimeout(() => {
      cleanup();
      reject(new Error("Audio metadata timed out."));
    }, 10_000);
    audio.src = previewUrl;
  });
}
