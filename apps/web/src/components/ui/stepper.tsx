import { CheckIcon } from "./icons";

export type StepperItem = {
  description?: string;
  id: string;
  label: string;
};

type StepperProps = {
  currentIndex: number;
  label?: string;
  steps: ReadonlyArray<StepperItem>;
};

export function Stepper({ currentIndex, label = "Progress", steps }: StepperProps) {
  return (
    <nav aria-label={label} className="stepper">
      <ol className="stepper__list">
        {steps.map((step, index) => {
          const status =
            index < currentIndex ? "complete" : index === currentIndex ? "current" : "upcoming";

          return (
            <li
              aria-current={status === "current" ? "step" : undefined}
              className={`stepper__item stepper__item--${status}`}
              key={step.id}
            >
              <span className="stepper__marker">
                {status === "complete" ? <CheckIcon /> : index + 1}
              </span>
              <span className="stepper__copy">
                <span className="stepper__label">{step.label}</span>
                {step.description ? (
                  <span className="stepper__description">{step.description}</span>
                ) : null}
              </span>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
