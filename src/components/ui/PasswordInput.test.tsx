import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import PasswordInput from "./PasswordInput";

describe("PasswordInput", () => {
  it("hides the value until the eye button is clicked, then hides it again", async () => {
    const user = userEvent.setup();
    render(<PasswordInput value="hunter2" onChange={() => {}} placeholder="pw" />);
    const input = screen.getByPlaceholderText("pw");
    expect(input).toHaveAttribute("type", "password");

    await user.click(screen.getByRole("button", { name: "Show" }));
    expect(input).toHaveAttribute("type", "text");

    await user.click(screen.getByRole("button", { name: "Hide" }));
    expect(input).toHaveAttribute("type", "password");
  });

  it("does not submit a surrounding form when toggled", async () => {
    const user = userEvent.setup();
    let submitted = false;
    render(
      <form onSubmit={(e) => { e.preventDefault(); submitted = true; }}>
        <PasswordInput value="" onChange={() => {}} />
      </form>,
    );
    await user.click(screen.getByRole("button", { name: "Show" }));
    expect(submitted).toBe(false);
  });
});
