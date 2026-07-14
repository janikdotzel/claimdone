import type {
  InputHTMLAttributes,
  ReactNode,
  TextareaHTMLAttributes,
} from "react";

import { classNames } from "./class-names";

type FieldShellProps = {
  children: ReactNode;
  description?: string | undefined;
  error?: string | undefined;
  id: string;
  label: string;
  optional?: boolean | undefined;
};

function FieldShell({
  children,
  description,
  error,
  id,
  label,
  optional = false,
}: FieldShellProps) {
  return (
    <div className={classNames("field", error && "field--error")}>
      <div className="field__label-row">
        <label className="field__label" htmlFor={id}>
          {label}
        </label>
        {optional ? <span className="field__optional">Optional</span> : null}
      </div>
      {description ? (
        <p className="field__description" id={`${id}-description`}>
          {description}
        </p>
      ) : null}
      {children}
      {error ? (
        <p className="field__error" id={`${id}-error`} role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function describedBy(id: string, description?: string, error?: string) {
  return (
    [description ? `${id}-description` : null, error ? `${id}-error` : null]
      .filter(Boolean)
      .join(" ") || undefined
  );
}

type TextInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, "id"> &
  Omit<FieldShellProps, "children">;

export function TextInput({
  className,
  description,
  error,
  id,
  label,
  optional,
  ...props
}: TextInputProps) {
  return (
    <FieldShell
      description={description}
      error={error}
      id={id}
      label={label}
      optional={optional}
    >
      <input
        aria-describedby={describedBy(id, description, error)}
        aria-invalid={error ? true : undefined}
        className={classNames("text-input", className)}
        id={id}
        {...props}
      />
    </FieldShell>
  );
}

type TextAreaProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "id"> &
  Omit<FieldShellProps, "children">;

export function TextArea({
  className,
  description,
  error,
  id,
  label,
  optional,
  ...props
}: TextAreaProps) {
  return (
    <FieldShell
      description={description}
      error={error}
      id={id}
      label={label}
      optional={optional}
    >
      <textarea
        aria-describedby={describedBy(id, description, error)}
        aria-invalid={error ? true : undefined}
        className={classNames("text-area", className)}
        id={id}
        {...props}
      />
    </FieldShell>
  );
}

type CheckboxFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "id" | "type"> & {
  description?: string | undefined;
  error?: string | undefined;
  id: string;
  label: string;
};

export function CheckboxField({
  className,
  description,
  error,
  id,
  label,
  ...props
}: CheckboxFieldProps) {
  return (
    <div className={classNames("checkbox-field", error && "field--error", className)}>
      <input
        aria-describedby={describedBy(id, description, error)}
        aria-invalid={error ? true : undefined}
        className="checkbox-field__control"
        id={id}
        type="checkbox"
        {...props}
      />
      <div>
        <label className="checkbox-field__label" htmlFor={id}>
          {label}
        </label>
        {description ? (
          <p className="checkbox-field__description" id={`${id}-description`}>
            {description}
          </p>
        ) : null}
        {error ? (
          <p className="field__error" id={`${id}-error`} role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}
