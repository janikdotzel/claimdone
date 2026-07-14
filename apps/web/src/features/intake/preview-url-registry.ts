export type ObjectUrlApi = Readonly<{
  createObjectURL: (blob: Blob) => string;
  revokeObjectURL: (url: string) => void;
}>;

export class PreviewUrlRegistry {
  readonly #active = new Set<string>();
  readonly #urlApi: ObjectUrlApi;

  constructor(urlApi: ObjectUrlApi) {
    this.#urlApi = urlApi;
  }

  create(blob: Blob) {
    const url = this.#urlApi.createObjectURL(blob);
    this.#active.add(url);
    return url;
  }

  release(url: string) {
    if (!this.#active.delete(url)) return false;
    this.#urlApi.revokeObjectURL(url);
    return true;
  }

  releaseAll() {
    for (const url of this.#active) this.#urlApi.revokeObjectURL(url);
    this.#active.clear();
  }

  get size() {
    return this.#active.size;
  }
}
