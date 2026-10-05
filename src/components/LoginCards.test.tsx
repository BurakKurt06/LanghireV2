import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import LoginCards from "./LoginCards";
import { getAuthStatus, launchLogin } from "../lib/api";

vi.mock("../lib/api", () => ({
  getAuthStatus: vi.fn(),
  launchLogin: vi.fn(),
}));

const mockGetAuthStatus = vi.mocked(getAuthStatus);
const mockLaunchLogin = vi.mocked(launchLogin);

const signedOut = { linkedin: { logged_in: false }, gmail: { logged_in: false } };

describe("LoginCards", () => {
  beforeEach(() => {
    mockGetAuthStatus.mockResolvedValue(signedOut as never);
    mockLaunchLogin.mockResolvedValue({ success: true, message: "Sign in to Gmail in the window that opened, then close it." } as never);
  });

  it("renders nothing until the status is known", () => {
    mockGetAuthStatus.mockReturnValue(new Promise(() => {}) as never);
    const { container } = render(<LoginCards />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows Gmail and LinkedIn as not signed in, with sign-in buttons", async () => {
    render(<LoginCards />);
    expect(await screen.findByText("Gmail")).toBeInTheDocument();
    expect(screen.getByText("LinkedIn")).toBeInTheDocument();
    expect(screen.getAllByText("Not signed in")).toHaveLength(2);
    expect(screen.getByRole("button", { name: /Sign in to Gmail/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sign in to LinkedIn/ })).toBeInTheDocument();
  });

  it("a signed-in service offers to sign in again", async () => {
    mockGetAuthStatus.mockResolvedValue({ linkedin: { logged_in: false }, gmail: { logged_in: true } } as never);
    render(<LoginCards />);
    expect(await screen.findByText("Signed in")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sign in again/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Sign in to Gmail/ })).not.toBeInTheDocument();
  });

  it("opens the sign-in window for the chosen service and shows what to do", async () => {
    const user = userEvent.setup();
    render(<LoginCards />);
    await user.click(await screen.findByRole("button", { name: /Sign in to Gmail/ }));
    await waitFor(() => expect(mockLaunchLogin).toHaveBeenCalledWith("gmail"));
    expect(await screen.findByRole("status")).toHaveTextContent("window that opened");
  });

  it("shows the reason inline when the window can't open", async () => {
    mockLaunchLogin.mockResolvedValue({ success: false, message: "Close the sign-in window first." } as never);
    const user = userEvent.setup();
    render(<LoginCards />);
    await user.click(await screen.findByRole("button", { name: /Sign in to LinkedIn/ }));
    expect(await screen.findByRole("status")).toHaveTextContent("Close the sign-in window first.");
  });

  it("shows a thrown error inline", async () => {
    mockLaunchLogin.mockRejectedValue(new Error("backend down"));
    const user = userEvent.setup();
    render(<LoginCards />);
    await user.click(await screen.findByRole("button", { name: /Sign in to Gmail/ }));
    expect(await screen.findByRole("status")).toHaveTextContent("backend down");
  });

  it("disables the buttons while a sign-in window is open", async () => {
    mockGetAuthStatus.mockResolvedValue({ ...signedOut, login_window_open: true } as never);
    render(<LoginCards />);
    expect(await screen.findByRole("button", { name: /Sign in to Gmail/ })).toBeDisabled();
    expect(screen.getByText(/A sign-in window is open/)).toBeInTheDocument();
  });

  it("checks the status again on request", async () => {
    const user = userEvent.setup();
    render(<LoginCards />);
    await screen.findByText("Gmail");
    const before = mockGetAuthStatus.mock.calls.length;
    await user.click(screen.getByRole("button", { name: /Check Gmail status again/ }));
    await waitFor(() => expect(mockGetAuthStatus.mock.calls.length).toBeGreaterThan(before));
  });
});
