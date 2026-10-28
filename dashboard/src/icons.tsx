// Small inline icon set (stroke icons, currentColor). Decorative: always aria-hidden; the control carries the label.
import type { ReactNode } from "react";

function Svg({ children, size = 20 }: { children: ReactNode; size?: number }) {
  return (
    <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {children}
    </svg>
  );
}

export const IconDevices = () => <Svg><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2V16h5v-.1c0-.8.4-1.5 1-2A6 6 0 0 0 12 3Z" /></Svg>;
export const IconChat = () => <Svg><path d="M21 12a8 8 0 0 1-11.7 7L4 20.5l1.5-4.6A8 8 0 1 1 21 12Z" /></Svg>;
export const IconAutomations = () => <Svg><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" /></Svg>;
export const IconActivity = () => <Svg><path d="M3 12h4l3-8 4 16 3-8h4" /></Svg>;
export const IconAdmin = () => <Svg><path d="M12 3 4 6v6c0 4.5 3.2 7.8 8 9 4.8-1.2 8-4.5 8-9V6l-8-3Z" /><path d="m9 12 2 2 4-4" /></Svg>;
export const IconBell = () => <Svg><path d="M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6ZM10 19a2 2 0 0 0 4 0" /></Svg>;
export const IconSun = () => <Svg><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></Svg>;
export const IconMoon = () => <Svg><path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5Z" /></Svg>;
export const IconAuto = () => <Svg><circle cx="12" cy="12" r="9" /><path d="M12 3v18" /><path d="M12 3a9 9 0 0 1 0 18Z" fill="currentColor" stroke="none" /></Svg>;
export const IconLogout = () => <Svg><path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 8l-4 4 4 4M6 12h10" /></Svg>;
export const IconSend = () => <Svg><path d="M12 19V5M5 12l7-7 7 7" /></Svg>;
export const IconStop = () => <svg className="icon" width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" focusable="false"><rect x="3" y="3" width="10" height="10" rx="2" /></svg>;
export const IconCopy = () => <Svg size={16}><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V6a2 2 0 0 1 2-2h8" /></Svg>;
export const IconCheck = () => <Svg size={16}><path d="m5 12 5 5 9-10" /></Svg>;
export const IconChevron = () => <Svg size={16}><path d="m9 6 6 6-6 6" /></Svg>;
export const IconMore = () => <Svg><circle cx="5" cy="12" r="1" fill="currentColor" /><circle cx="12" cy="12" r="1" fill="currentColor" /><circle cx="19" cy="12" r="1" fill="currentColor" /></Svg>;
export const IconDown = () => <Svg size={16}><path d="M12 5v14M5 12l7 7 7-7" /></Svg>;
