import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { copy, isLocale } from "@/lib/locales";
import "../style.css";

export const metadata: Metadata = { title: "Forty Hadith | A Waqf Project", description: "Lessons from Sheikh Ibn Uthaymeen's explanation of Imam An-Nawawi's Forty Hadith." };
export default function RootLayout({ children, params }: { children: React.ReactNode; params: { locale: string } }) {
  if (!isLocale(params.locale)) notFound();
  const locale = params.locale;
  return <html lang={locale} dir={locale === "ar" || locale === "ur" ? "rtl" : "ltr"}><body>
    <div className="shell"><header className="site-header"><a href={`/${locale}`} className="brand">{copy[locale].title}<span>WAQF · وقف</span></a></header>
    {children}
    <footer><strong>{copy[locale].waqf}</strong><p>{copy[locale].waqfText}</p></footer></div>
  </body></html>;
}
