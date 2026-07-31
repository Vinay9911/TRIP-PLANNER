"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import Image from "next/image";
import { getSupabase } from "@/lib/supabase";
import {
  IconBed,
  IconBrain,
  IconChat,
  IconChevron,
  IconClock,
  IconCompass,
  IconFork,
  IconPin,
  IconPlane,
  IconSparkle,
  IconSun,
  IconTicket,
  IconShield,
  IconCalendar,
} from "@/components/icons";

export default function LandingPage() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null);

  useEffect(() => {
    getSupabase()
      .auth.getSession()
      .then(({ data }) => setSignedIn(Boolean(data.session)))
      .catch(() => setSignedIn(false));
  }, []);

  const cta = signedIn ? "Open the planner" : "Start planning";
  const href = signedIn ? "/chat" : "/login";

  return (
    <div className="mx-auto w-full max-w-[1400px] px-4 pb-12 sm:px-8 relative overflow-hidden bg-[#fafbfe]" style={{ backgroundImage: "radial-gradient(#e5e7eb 1px, transparent 1px)", backgroundSize: "30px 30px" }}>
      {/* Background Soft Blobs */}
      <div className="absolute top-0 left-0 w-[800px] h-[800px] bg-rose-100/40 rounded-full blur-[120px] -translate-x-1/2 -translate-y-1/2 pointer-events-none" />
      <div className="absolute top-[20%] right-0 w-[600px] h-[600px] bg-purple-100/40 rounded-full blur-[100px] translate-x-1/3 pointer-events-none" />
      <div className="absolute bottom-[20%] left-[20%] w-[500px] h-[500px] bg-orange-50/50 rounded-full blur-[100px] pointer-events-none" />

      {/* Header */}
      <header className="flex items-center justify-between py-6 relative z-10">
        <div className="flex items-center gap-3">
          <div className="bg-[#e85d2c] text-white w-10 h-10 flex items-center justify-center rounded-xl shadow-[0_4px_12px_rgba(232,93,44,0.3)]">
            <IconPlane size="1.2em" />
          </div>
          <div>
            <span className="font-display font-bold text-xl leading-none flex items-center gap-2">
              Trip Planner <span className="text-[10px] bg-purple-100 text-purple-600 px-2 py-0.5 rounded-full font-bold uppercase tracking-wider">AI</span>
            </span>
            <span className="text-[11px] text-gray-500 font-medium leading-none block mt-1">Your AI Travel Companion</span>
          </div>
        </div>
        
        <div className="hidden lg:flex items-center gap-8 text-[13px] font-semibold text-gray-600">
          <span className="flex items-center gap-2 hover:text-purple-600 cursor-pointer transition-colors"><IconBrain size="1.2em" className="text-purple-500" /> AI Powered</span>
          <span className="flex items-center gap-2 hover:text-pink-500 cursor-pointer transition-colors"><IconSparkle size="1.2em" className="text-pink-400" /> Smart Memory</span>
          <span className="flex items-center gap-2 hover:text-blue-500 cursor-pointer transition-colors"><IconCompass size="1.2em" className="text-blue-500" /> Real-time Data</span>
          <span className="flex items-center gap-2 hover:text-yellow-600 cursor-pointer transition-colors"><IconShield size="1.2em" className="text-yellow-500" /> Secure & Private</span>
        </div>

        <Link href={href} className="bg-gradient-to-r from-[#e85d2c] to-[#f97316] text-white text-sm font-bold px-6 py-3 rounded-xl shadow-[0_8px_20px_-6px_rgba(232,93,44,0.6)] hover:shadow-[0_12px_25px_-6px_rgba(232,93,44,0.7)] hover:-translate-y-0.5 transition-all flex items-center gap-1.5">
          {signedIn === null ? "Continue" : "Open the planner"} <IconChevron size="1.2em" />
        </Link>
      </header>

      {/* Hero Section */}
      <section className="pt-6 pb-6 relative z-10">
        <div className="grid lg:grid-cols-[1fr_1.2fr] gap-8 items-center">
          
          {/* Left Content */}
          <div className="max-w-xl">
            <div className="inline-flex items-center gap-1.5 bg-orange-50 text-orange-600 px-3 py-1.5 rounded-full text-xs font-bold border border-orange-100 mb-8 shadow-sm">
              <IconSparkle size="1em" />
              The intelligent travel assistant
            </div>

            <h1 className="font-display text-6xl sm:text-[80px] font-bold leading-[1.05] tracking-tight text-[#1a1a1a]">
              Plan your next<br/>
              <span className="bg-gradient-to-r from-[#e85d2c] via-[#db2777] to-[#7c3aed] bg-clip-text text-transparent pb-2 block animate-text-gradient">
                adventure
              </span>
              in seconds.
            </h1>

            <p className="mt-6 text-[17px] leading-relaxed text-gray-500 font-medium max-w-md">
              An AI travel agent that researches real guides, asks smart questions, and remembers your preferences to build the perfect, personalized itinerary.
            </p>

            <div className="mt-10 flex flex-wrap items-center gap-4">
              <Link
                href={href}
                className="bg-[#e85d2c] text-white text-[15px] font-bold px-8 py-4 rounded-xl shadow-[0_8px_25px_-8px_rgba(232,93,44,0.7)] hover:-translate-y-1 hover:shadow-[0_15px_35px_-8px_rgba(232,93,44,0.8)] transition-all flex items-center gap-2"
              >
                {signedIn === null ? "Continue" : "Open the planner"} <IconChevron size="1.1em" />
              </Link>
              
              <a href="#how" className="bg-white text-gray-700 border border-gray-200 text-[15px] font-bold px-6 py-4 rounded-xl shadow-sm hover:border-gray-300 hover:bg-gray-50 transition-all flex items-center gap-2">
                See how it works 
                <div className="w-5 h-5 rounded-full border-2 border-gray-700 flex items-center justify-center pl-0.5">
                  <div className="w-0 h-0 border-t-4 border-t-transparent border-l-[6px] border-l-gray-700 border-b-4 border-b-transparent"></div>
                </div>
              </a>
            </div>

            {/* Feature Pills */}
            <div className="mt-14 grid grid-cols-2 sm:grid-cols-4 gap-3">
              <div className="bg-white/60 backdrop-blur-sm border border-gray-100 rounded-xl p-3 flex flex-col gap-1 shadow-sm">
                <div className="flex items-center gap-1.5 text-[11px] font-bold text-gray-700"><IconBrain size="1.2em" className="text-pink-500"/> Remembers you</div>
                <div className="text-[10px] text-gray-400 font-medium">Learns your preferences</div>
              </div>
              <div className="bg-white/60 backdrop-blur-sm border border-gray-100 rounded-xl p-3 flex flex-col gap-1 shadow-sm">
                <div className="flex items-center gap-1.5 text-[11px] font-bold text-gray-700"><IconClock size="1.2em" className="text-yellow-500"/> Saves hours</div>
                <div className="text-[10px] text-gray-400 font-medium">Research in seconds</div>
              </div>
              <div className="bg-white/60 backdrop-blur-sm border border-gray-100 rounded-xl p-3 flex flex-col gap-1 shadow-sm">
                <div className="flex items-center gap-1.5 text-[11px] font-bold text-gray-700"><IconCompass size="1.2em" className="text-emerald-500"/> Real-time info</div>
                <div className="text-[10px] text-gray-400 font-medium">Always up to date</div>
              </div>
              <div className="bg-white/60 backdrop-blur-sm border border-gray-100 rounded-xl p-3 flex flex-col gap-1 shadow-sm">
                <div className="flex items-center gap-1.5 text-[11px] font-bold text-gray-700"><IconShield size="1.2em" className="text-orange-400"/> Private & secure</div>
                <div className="text-[10px] text-gray-400 font-medium">Your data, your control</div>
              </div>
            </div>
          </div>

          {/* Right Content: The Massive Dashboard Mockup */}
          <div className="relative h-[650px] w-full hidden lg:block">
            {/* Faint white background box for the mockup */}
            <div className="absolute top-4 -right-12 w-[110%] h-[95%] bg-white/40 rounded-[40px] shadow-[0_20px_60px_-15px_rgba(0,0,0,0.05)] border border-white/60 backdrop-blur-md"></div>
            
            {/* Main App Window */}
            <div className="absolute top-12 left-0 right-0 h-[500px] bg-white rounded-3xl shadow-[0_30px_100px_-20px_rgba(0,0,0,0.12)] border border-gray-100 overflow-hidden flex flex-col z-10">
              
              {/* App Header */}
              <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between bg-white">
                <div className="flex items-center gap-4">
                  <div className="w-12 h-12 bg-red-50 text-red-500 flex items-center justify-center rounded-xl border border-red-100">
                    <span className="text-2xl font-bold font-serif">⛩</span>
                  </div>
                  <div>
                    <h2 className="font-bold text-lg text-gray-900 leading-tight">Tokyo Adventure</h2>
                    <p className="text-xs font-medium text-gray-400 flex items-center gap-1">
                      <IconPin size="1em"/> Tokyo, Japan <span className="mx-1">•</span> 2 Days Trip
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-6">
                  <div className="flex items-center gap-2">
                    <IconSun size="2em" className="text-yellow-400"/>
                    <div>
                      <div className="font-bold text-sm text-gray-900 leading-none">28°C</div>
                      <div className="text-[10px] font-medium text-gray-400 mt-1">Clear</div>
                    </div>
                  </div>
                  <div className="w-10 h-10 bg-gray-100 rounded-full overflow-hidden border border-gray-200 flex items-center justify-center">
                    <div className="w-full h-full bg-gray-300">
                      {/* Fake Avatar */}
                      <svg viewBox="0 0 100 100" className="w-full h-full text-gray-400 fill-current"><path d="M50 50c13.8 0 25-11.2 25-25S63.8 0 50 0 25 11.2 25 25s11.2 25 25 25zm0 10c-16.7 0-50 8.3-50 25v15h100V85c0-16.7-33.3-25-50-25z"/></svg>
                    </div>
                  </div>
                </div>
              </div>

              <div className="flex flex-1 overflow-hidden bg-[#fafafa]">
                {/* Sidebar */}
                <div className="w-48 bg-white border-r border-gray-100 p-4 space-y-1">
                  <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg bg-orange-50 text-[#e85d2c] font-bold text-sm"><IconCompass size="1.2em"/> Overview</div>
                  <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-gray-500 font-medium text-sm hover:bg-gray-50"><IconClock size="1.2em"/> Itinerary</div>
                  <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-gray-500 font-medium text-sm hover:bg-gray-50"><IconPin size="1.2em"/> Map</div>
                  <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-gray-500 font-medium text-sm hover:bg-gray-50"><IconPlane size="1.2em"/> Flights</div>
                  <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-gray-500 font-medium text-sm hover:bg-gray-50"><IconBed size="1.2em"/> Hotels</div>
                  <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-gray-500 font-medium text-sm hover:bg-gray-50"><IconTicket size="1.2em"/> Budget</div>
                  <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-gray-500 font-medium text-sm hover:bg-gray-50"><IconBrain size="1.2em"/> Memories</div>
                </div>

                {/* Main Content Area */}
                <div className="flex-1 p-6 flex gap-6 overflow-hidden">
                  
                  {/* Left Data Column */}
                  <div className="flex-1 flex flex-col gap-6">
                    <div>
                      <h3 className="font-bold text-xl text-gray-900 mb-1">Your AI Trip is ready! 🎉</h3>
                      <p className="text-xs text-gray-500 font-medium leading-relaxed max-w-xs">A personalized 2-day Tokyo itinerary based on your preferences.</p>
                    </div>

                    <div className="grid grid-cols-4 gap-3">
                      <div className="bg-white border border-gray-100 rounded-xl p-3 flex flex-col items-center justify-center text-center shadow-sm">
                        <span className="text-xl font-bold text-emerald-500">12</span>
                        <span className="text-[9px] font-bold text-gray-400 mt-1 uppercase">Attractions</span>
                      </div>
                      <div className="bg-white border border-gray-100 rounded-xl p-3 flex flex-col items-center justify-center text-center shadow-sm">
                        <span className="text-xl font-bold text-purple-500">8</span>
                        <span className="text-[9px] font-bold text-gray-400 mt-1 uppercase">Restaurants</span>
                      </div>
                      <div className="bg-white border border-gray-100 rounded-xl p-3 flex flex-col items-center justify-center text-center shadow-sm">
                        <span className="text-xl font-bold text-orange-500">2</span>
                        <span className="text-[9px] font-bold text-gray-400 mt-1 uppercase">Day Plan</span>
                      </div>
                      <div className="bg-white border border-gray-100 rounded-xl p-3 flex flex-col items-center justify-center text-center shadow-sm bg-gray-50">
                        <span className="text-xl font-bold text-gray-800">5.0</span>
                        <span className="text-[9px] font-bold text-gray-400 mt-1 uppercase">AI Score</span>
                      </div>
                    </div>

                    <div className="bg-white border border-gray-100 rounded-xl p-5 shadow-sm mt-auto">
                      <h4 className="text-xs font-bold text-gray-900 mb-5">AI is researching the best for you...</h4>
                      <div className="flex justify-between items-center relative">
                        <div className="absolute top-3 left-6 right-6 h-0.5 bg-gray-100 z-0"></div>
                        <div className="absolute top-3 left-6 right-1/2 h-0.5 bg-emerald-400 z-0"></div>
                        
                        <div className="flex flex-col items-center gap-2 relative z-10">
                          <div className="w-6 h-6 rounded-full bg-emerald-400 text-white flex items-center justify-center"><IconCompass size="12"/></div>
                          <span className="text-[9px] font-bold text-emerald-500 uppercase tracking-wide">Search</span>
                        </div>
                        <div className="flex flex-col items-center gap-2 relative z-10">
                          <div className="w-6 h-6 rounded-full bg-emerald-400 text-white flex items-center justify-center"><IconSun size="12"/></div>
                          <span className="text-[9px] font-bold text-emerald-500 uppercase tracking-wide">Weather</span>
                        </div>
                        <div className="flex flex-col items-center gap-2 relative z-10">
                          <div className="w-10 h-10 rounded-full bg-purple-100 text-purple-600 border-[3px] border-white shadow-md flex items-center justify-center -mt-2"><IconTicket size="20"/></div>
                          <span className="text-[10px] font-bold text-purple-600 uppercase tracking-wide">Attractions</span>
                        </div>
                        <div className="flex flex-col items-center gap-2 relative z-10">
                          <div className="w-6 h-6 rounded-full bg-white border-2 border-gray-200 text-gray-300 flex items-center justify-center"><IconBed size="12"/></div>
                          <span className="text-[9px] font-bold text-gray-300 uppercase tracking-wide">Hotels</span>
                        </div>
                        <div className="flex flex-col items-center gap-2 relative z-10">
                          <div className="w-6 h-6 rounded-full bg-white border-2 border-gray-200 text-gray-300 flex items-center justify-center"><IconClock size="12"/></div>
                          <span className="text-[9px] font-bold text-gray-300 uppercase tracking-wide">Itinerary</span>
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* Right Map Area */}
                  <div className="w-56 bg-blue-50 rounded-xl border border-gray-200 overflow-hidden relative shadow-inner">
                    {/* Fake Map Background using generic pattern/image */}
                    <img src="/tokyo-map.png" alt="Map" className="absolute inset-0 w-full h-full object-cover opacity-90" />
                    
                    {/* Path SVG */}
                    <svg className="absolute inset-0 w-full h-full" style={{strokeDasharray: "4 4"}}>
                       <path d="M 60,180 Q 100,140 180,60" fill="transparent" stroke="#7c3aed" strokeWidth="2" />
                    </svg>
                    
                    <div className="absolute top-14 right-10 flex flex-col items-center">
                      <div className="bg-white text-xs font-bold px-2 py-1 rounded shadow-sm mb-1 z-10 text-nowrap">Tokyo Skytree</div>
                      <div className="w-5 h-5 bg-purple-500 rounded-full text-white flex items-center justify-center shadow-lg border-2 border-white"><IconPin size="12"/></div>
                    </div>
                    
                    <div className="absolute top-1/2 left-8 flex flex-col items-center">
                      <div className="w-3 h-3 bg-white rounded-full border-2 border-purple-500 shadow-md"></div>
                      <div className="bg-white/80 backdrop-blur text-[10px] font-bold px-1.5 py-0.5 rounded shadow-sm mt-1">Senso-ji Temple</div>
                    </div>

                    <div className="absolute bottom-20 left-16 flex flex-col items-center">
                      <div className="w-5 h-5 bg-[#e85d2c] rounded-full text-white flex items-center justify-center shadow-lg border-2 border-white"><IconPin size="12"/></div>
                      <div className="bg-white text-xs font-bold px-2 py-1 rounded shadow-sm mt-1 z-10 text-nowrap">Shibuya Crossing</div>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {/* Floating Photos (Polaroids) Grouped Together */}
            <div className="absolute -bottom-10 left-6 w-36 bg-white p-2 pb-5 rounded-xl shadow-[0_15px_30px_-5px_rgba(0,0,0,0.15)] border border-gray-100 -rotate-[10deg] z-20 transition-transform hover:scale-105 hover:rotate-0 hover:z-50 cursor-pointer">
              <div className="w-full h-24 bg-gray-200 rounded-lg overflow-hidden relative">
                <img src="/sensoji.png" className="object-cover w-full h-full" alt="Senso-ji"/>
              </div>
              <div className="mt-2 flex justify-between items-start px-1">
                <div>
                  <h5 className="text-[10px] font-bold text-gray-900 leading-none">Senso-ji Temple</h5>
                  <p className="text-[8px] text-gray-500 mt-1">Historic Temple</p>
                </div>
                <div className="flex items-center text-[9px] font-bold text-yellow-500 gap-0.5"><span className="text-yellow-400">★</span> 4.8</div>
              </div>
            </div>

            <div className="absolute -bottom-6 left-28 w-40 bg-white p-2 pb-5 rounded-xl shadow-[0_20px_40px_-5px_rgba(0,0,0,0.2)] border border-gray-100 z-30 transition-transform hover:scale-105 hover:z-50 cursor-pointer">
              <div className="w-full h-28 bg-gray-200 rounded-lg overflow-hidden relative">
                <img src="/tokyo-tower.png" className="object-cover w-full h-full" alt="Tokyo Tower"/>
              </div>
              <div className="mt-2 flex justify-between items-start px-1">
                <div>
                  <h5 className="text-[11px] font-bold text-gray-900 leading-none">Tokyo Tower</h5>
                  <p className="text-[9px] text-gray-500 mt-1">City Landmark</p>
                </div>
                <div className="flex items-center text-[10px] font-bold text-yellow-500 gap-0.5"><span className="text-yellow-400">★</span> 4.7</div>
              </div>
            </div>

            <div className="absolute -bottom-12 left-52 w-36 bg-white p-2 pb-5 rounded-xl shadow-[0_15px_30px_-5px_rgba(0,0,0,0.15)] border border-gray-100 rotate-[8deg] z-20 transition-transform hover:scale-105 hover:rotate-0 hover:z-50 cursor-pointer">
              <div className="w-full h-24 bg-gray-200 rounded-lg overflow-hidden relative">
                <img src="/shibuya.png" className="object-cover w-full h-full" alt="Shibuya"/>
              </div>
              <div className="mt-2 flex justify-between items-start px-1">
                <div>
                  <h5 className="text-[10px] font-bold text-gray-900 leading-none">Shibuya Crossing</h5>
                  <p className="text-[8px] text-gray-500 mt-1">Must See</p>
                </div>
                <div className="flex items-center text-[9px] font-bold text-yellow-500 gap-0.5"><span className="text-yellow-400">★</span> 4.9</div>
              </div>
            </div>

            {/* AI Memory Floating Card */}
            <div className="absolute top-1/2 -right-8 bg-white p-4 rounded-2xl shadow-[0_20px_50px_-10px_rgba(0,0,0,0.15)] border border-gray-100 rotate-3 z-40 w-48 transition-transform hover:scale-105 hover:rotate-0">
              <div className="flex items-center gap-2 mb-3 border-b border-gray-50 pb-2">
                <IconBrain size="1.2em" className="text-purple-600"/>
                <h4 className="text-xs font-bold text-gray-900">AI Memory Active</h4>
              </div>
              <ul className="space-y-2">
                <li className="flex items-center gap-2 text-[11px] font-medium text-gray-600"><span className="text-emerald-500">✓</span> Vegetarian</li>
                <li className="flex items-center gap-2 text-[11px] font-medium text-gray-600"><span className="text-emerald-500">✓</span> Budget Traveller</li>
                <li className="flex items-center gap-2 text-[11px] font-medium text-gray-600"><span className="text-emerald-500">✓</span> Loves Museums</li>
                <li className="flex items-center gap-2 text-[11px] font-medium text-gray-600"><span className="text-emerald-500">✓</span> Prefers Metro</li>
              </ul>
            </div>

          </div>
        </div>
      </section>

      {/* Stats Bar */}
      <section className="mt-8 bg-white rounded-3xl shadow-sm border border-gray-100 py-6 px-10 relative z-20 max-w-[1200px] mx-auto">
        <div className="absolute -top-3 left-1/2 -translate-x-1/2 bg-[#fafbfe] px-4 text-[10px] font-bold text-gray-400 uppercase tracking-widest border border-gray-100 rounded-full shadow-sm">Trusted by travelers worldwide</div>
        
        <div className="flex flex-wrap justify-between items-center gap-6">
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 bg-purple-50 rounded-full flex items-center justify-center text-purple-600"><IconCompass size="1.5em"/></div>
            <div>
              <div className="text-2xl font-bold text-gray-900 leading-tight">50K+</div>
              <div className="text-xs text-gray-500 font-medium">Happy Travelers</div>
            </div>
          </div>
          
          <div className="w-px h-12 bg-gray-100 hidden md:block"></div>
          
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 bg-orange-50 rounded-full flex items-center justify-center text-orange-500"><IconTicket size="1.5em"/></div>
            <div>
              <div className="text-2xl font-bold text-gray-900 leading-tight">120+</div>
              <div className="text-xs text-gray-500 font-medium">Countries Explored</div>
            </div>
          </div>

          <div className="w-px h-12 bg-gray-100 hidden md:block"></div>
          
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 bg-pink-50 rounded-full flex items-center justify-center text-pink-500"><IconCalendar size="1.5em"/></div>
            <div>
              <div className="text-2xl font-bold text-gray-900 leading-tight">1M+</div>
              <div className="text-xs text-gray-500 font-medium">Trips Planned</div>
            </div>
          </div>

          <div className="w-px h-12 bg-gray-100 hidden md:block"></div>
          
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 bg-yellow-50 rounded-full flex items-center justify-center text-yellow-500"><IconSparkle size="1.5em"/></div>
            <div>
              <div className="text-2xl font-bold text-gray-900 leading-tight">4.9/5</div>
              <div className="text-xs text-gray-500 font-medium">User Rating</div>
            </div>
          </div>
        </div>
      </section>

      {/* -- How it works ---------------------------------------------------- */}
      <section id="how" className="mt-16 relative z-10 max-w-[1200px] mx-auto">
        <div className="text-center mb-10">
          <div className="flex items-center justify-center gap-2 mb-4">
            <IconSparkle className="text-purple-400" size="1.5em" />
            <h2 className="font-display text-4xl font-bold tracking-tight text-gray-900">
              How it works
            </h2>
          </div>
          <div className="mx-auto w-12 h-1 bg-gradient-to-r from-orange-400 to-purple-500 rounded-full mb-6"></div>
          <p className="mx-auto max-w-2xl text-[15px] font-medium text-gray-500 leading-relaxed">
            Most assistants either interrogate you with forms or guess randomly.<br/>
            This one picks a gear based on what you say — acting like a real human travel agent.
          </p>
        </div>

        <div className="relative grid gap-8 md:grid-cols-3 pb-8">
          {/* Connecting SVG Line behind cards */}
          <svg className="absolute top-1/2 left-0 w-full h-[100px] -translate-y-1/2 -z-10 hidden md:block opacity-30" preserveAspectRatio="none" viewBox="0 0 1000 100">
            <path d="M0,50 Q200,-50 330,50 T660,50 T1000,50" fill="none" stroke="url(#grad)" strokeWidth="3" strokeDasharray="8 8"/>
            <defs>
              <linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="0%">
                <stop offset="0%" stopColor="#e85d2c" />
                <stop offset="50%" stopColor="#7c3aed" />
                <stop offset="100%" stopColor="#e85d2c" />
              </linearGradient>
            </defs>
          </svg>

          {/* Workflow 1: Clarify */}
          <div className="bg-white rounded-[2rem] p-6 shadow-[0_10px_40px_-10px_rgba(0,0,0,0.08)] border border-orange-100 relative overflow-hidden group hover:-translate-y-2 transition-transform duration-300">
            {/* Cityscape bottom pattern */}
            <div className="absolute bottom-0 left-0 right-0 h-32 opacity-[0.05] bg-[url('data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiPjxyZWN0IHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiIGZpbGw9IiNlODVkMmMiLz48L3N2Zz4=')] mix-blend-multiply pointer-events-none" style={{backgroundSize: 'cover', backgroundPosition: 'bottom'}}>
              <div className="w-full h-full flex items-end gap-1 opacity-20">
                <div className="w-4 h-10 bg-[#e85d2c]"></div><div className="w-6 h-16 bg-[#e85d2c]"></div><div className="w-8 h-8 bg-[#e85d2c] rounded-t-full"></div><div className="w-3 h-24 bg-[#e85d2c]"></div><div className="w-5 h-12 bg-[#e85d2c]"></div>
              </div>
            </div>

            <div className="flex items-start justify-between relative z-10 mb-8">
              <div className="flex items-center gap-3">
                <div className="w-8 h-8 bg-[#e85d2c] text-white rounded-xl flex items-center justify-center font-bold shadow-md">1</div>
                <div>
                  <h3 className="font-bold text-gray-900 text-lg leading-tight">Clarify</h3>
                  <p className="text-[9px] font-bold text-gray-400 uppercase tracking-widest mt-0.5">When you're vague</p>
                </div>
              </div>
              <div className="w-10 h-10 bg-orange-50 rounded-full flex items-center justify-center text-orange-400"><IconChat size="1.2em"/></div>
            </div>
            
            <div className="space-y-4 relative z-10">
              <div className="flex justify-end">
                <div className="bg-gray-900 text-white text-sm font-medium px-5 py-3 rounded-2xl rounded-br-sm shadow-md max-w-[85%]">
                  Find me flights to London.
                </div>
              </div>
              <div className="flex justify-start">
                <div className="bg-white border border-gray-100 text-gray-800 text-sm font-medium px-5 py-3 rounded-2xl rounded-bl-sm shadow-md max-w-[85%] flex gap-2">
                  <IconChat size="1.2em" className="text-orange-400 mt-0.5 shrink-0"/>
                  Which city will you be flying from? ✈️
                </div>
              </div>
            </div>
          </div>

          {/* Workflow 2: Advise */}
          <div className="bg-white rounded-[2rem] p-6 shadow-[0_10px_40px_-10px_rgba(0,0,0,0.08)] border border-purple-100 relative overflow-hidden group hover:-translate-y-2 transition-transform duration-300">
            {/* Palm tree bottom pattern */}
            <div className="absolute bottom-0 left-0 right-0 h-32 opacity-[0.05] mix-blend-multiply pointer-events-none flex justify-end items-end p-2">
              <div className="w-16 h-16 bg-purple-600 rounded-tl-full"></div>
            </div>

            <div className="flex items-start justify-between relative z-10 mb-8">
              <div className="flex items-center gap-3">
                <div className="w-8 h-8 bg-purple-600 text-white rounded-xl flex items-center justify-center font-bold shadow-md">2</div>
                <div>
                  <h3 className="font-bold text-gray-900 text-lg leading-tight">Advise</h3>
                  <p className="text-[9px] font-bold text-gray-400 uppercase tracking-widest mt-0.5">When exploring options</p>
                </div>
              </div>
              <div className="w-10 h-10 bg-purple-50 rounded-full flex items-center justify-center text-purple-400"><IconSparkle size="1.2em"/></div>
            </div>
            
            <div className="space-y-4 relative z-10">
              <div className="flex justify-end">
                <div className="bg-purple-600 text-white text-sm font-medium px-5 py-3 rounded-2xl rounded-br-sm shadow-md max-w-[85%]">
                  I want to go to Kerala
                </div>
              </div>
              <div className="flex justify-start">
                <div className="bg-white border border-gray-100 text-gray-800 px-5 py-4 rounded-2xl rounded-bl-sm shadow-md w-full">
                  <p className="font-bold text-sm mb-3">Kerala splits into a few trips 🌴</p>
                  <div className="space-y-2 text-[11px] font-medium text-gray-500">
                    <p><strong className="text-gray-900">Backwaters</strong> — Alleppey houseboats</p>
                    <p><strong className="text-gray-900">Tea hills</strong> — Munnar plantations</p>
                  </div>
                  <div className="mt-4 flex gap-2">
                    <span className="bg-purple-50 text-purple-700 px-3 py-1.5 rounded-full text-[10px] font-bold shadow-sm">Alleppey</span>
                    <span className="bg-gray-50 text-gray-600 px-3 py-1.5 rounded-full text-[10px] font-bold shadow-sm border border-gray-100">Munnar</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Workflow 3: Plan */}
          <div className="bg-white rounded-[2rem] p-6 shadow-[0_10px_40px_-10px_rgba(0,0,0,0.08)] border border-orange-100 relative overflow-hidden group hover:-translate-y-2 transition-transform duration-300">
            {/* Pagoda bottom pattern */}
            <div className="absolute bottom-0 left-0 right-0 h-32 opacity-[0.05] mix-blend-multiply pointer-events-none flex justify-center items-end">
               <div className="w-32 h-20 border-t-[10px] border-[#e85d2c] flex flex-col items-center">
                 <div className="w-24 h-10 border-t-[8px] border-[#e85d2c]"></div>
               </div>
            </div>

            <div className="flex items-start justify-between relative z-10 mb-8">
              <div className="flex items-center gap-3">
                <div className="w-8 h-8 bg-[#e85d2c] text-white rounded-xl flex items-center justify-center font-bold shadow-md">3</div>
                <div>
                  <h3 className="font-bold text-gray-900 text-lg leading-tight">Plan</h3>
                  <p className="text-[9px] font-bold text-gray-400 uppercase tracking-widest mt-0.5">When you're ready</p>
                </div>
              </div>
              <div className="w-10 h-10 bg-orange-50 rounded-full flex items-center justify-center text-orange-400"><IconCalendar size="1.2em"/></div>
            </div>
            
            <div className="space-y-4 relative z-10">
              <div className="flex justify-end">
                <div className="bg-[#e85d2c] text-white text-sm font-medium px-5 py-3 rounded-2xl rounded-br-sm shadow-md max-w-[85%]">
                  Plan me 2 days in Kyoto
                </div>
              </div>
              <div className="flex justify-start">
                <div className="bg-white border border-gray-100 rounded-2xl shadow-md w-full overflow-hidden">
                  <div className="bg-gray-50 px-4 py-3 flex items-center justify-between border-b border-gray-100">
                    <span className="font-bold text-xs flex items-center gap-2"><span className="bg-orange-500 text-white w-5 h-5 rounded flex items-center justify-center text-[10px] shadow-sm">1</span> Kyoto Classic</span>
                    <IconChevron size="1.2em" className="text-gray-400"/>
                  </div>
                  <div className="p-3 bg-white">
                    <div className="flex gap-3 items-center bg-orange-50 p-2.5 rounded-xl border border-orange-100">
                      <div className="w-10 h-10 rounded-lg bg-orange-100 flex items-center justify-center text-orange-500 shrink-0 shadow-inner"><IconTicket size="1.2em"/></div>
                      <div>
                        <p className="text-xs font-bold text-gray-900">Fushimi Inari Taisha</p>
                        <p className="text-[10px] font-medium text-gray-500 mt-0.5">Higashiyama • 2 hours</p>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Bento Box: Memory & Live Data */}
      <section className="mt-12 max-w-[1200px] mx-auto">
        <div className="grid lg:grid-cols-2 gap-6">
          
          {/* Memory Section */}
          <div className="bg-white rounded-[2rem] p-8 shadow-sm border border-gray-100 flex flex-col md:flex-row gap-8 items-center hover:shadow-md transition-shadow">
            <div className="flex-1">
              <span className="inline-flex items-center gap-2 text-purple-600 mb-3">
                <IconBrain size="1.2em" />
                <span className="font-bold text-[10px] tracking-widest uppercase">Long-Term Memory</span>
              </span>
              <h3 className="font-display text-4xl font-bold text-gray-900 tracking-tight">
                Say it once.
              </h3>
              <p className="mt-4 text-[15px] font-medium leading-relaxed text-gray-500">
                Mention you're vegetarian or fly from Delhi, and every future trip is planned around it. Hard requirements are passed to searches as filters, not just hints.
              </p>
            </div>
            
            <div className="flex-1 w-full flex flex-col gap-3">
              <div className="flex items-center gap-3 bg-white border border-gray-100 rounded-xl px-5 py-3.5 shadow-[0_4px_15px_-5px_rgba(0,0,0,0.05)] hover:-translate-x-1 transition-transform">
                <IconFork size="1.2em" className="text-purple-500"/>
                <span className="font-bold text-sm text-gray-700">Vegetarian</span>
              </div>
              <div className="flex items-center gap-3 bg-white border border-gray-100 rounded-xl px-5 py-3.5 shadow-[0_4px_15px_-5px_rgba(0,0,0,0.05)] hover:-translate-x-1 transition-transform ml-4">
                <IconPin size="1.2em" className="text-purple-500"/>
                <span className="font-bold text-sm text-gray-700">Quiet places</span>
              </div>
              <div className="flex items-center gap-3 bg-white border border-gray-100 rounded-xl px-5 py-3.5 shadow-[0_4px_15px_-5px_rgba(0,0,0,0.05)] hover:-translate-x-1 transition-transform">
                <IconPlane size="1.2em" className="text-[#e85d2c]"/>
                <span className="font-bold text-sm text-gray-700">Flies from Delhi</span>
              </div>
            </div>
          </div>

          {/* Live Data Section */}
          <div className="bg-white rounded-[2rem] p-8 shadow-sm border border-gray-100 flex flex-col justify-between hover:shadow-md transition-shadow">
            <div>
              <span className="inline-flex items-center gap-2 text-[#e85d2c] mb-3">
                <IconCompass size="1.2em" />
                <span className="font-bold text-[10px] tracking-widest uppercase">Live Data</span>
              </span>
              <h3 className="font-display text-4xl font-bold text-gray-900 tracking-tight">
                Real sources.
              </h3>
            </div>
            
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-8">
              <div className="bg-gray-50 border border-gray-100 rounded-2xl p-4 flex flex-col items-center justify-center text-center hover:bg-white hover:shadow-lg transition-all group">
                <div className="w-12 h-12 bg-white rounded-xl shadow-sm flex items-center justify-center text-gray-400 group-hover:text-[#e85d2c] transition-colors mb-3">
                  <IconCompass size="1.5em"/>
                </div>
                <div className="font-bold text-xs text-gray-900">Travel guides</div>
                <div className="text-[9px] font-medium text-gray-400 mt-1">Wikivoyage</div>
              </div>
              <div className="bg-gray-50 border border-gray-100 rounded-2xl p-4 flex flex-col items-center justify-center text-center hover:bg-white hover:shadow-lg transition-all group">
                <div className="w-12 h-12 bg-white rounded-xl shadow-sm flex items-center justify-center text-gray-400 group-hover:text-emerald-500 transition-colors mb-3">
                  <IconPin size="1.5em"/>
                </div>
                <div className="font-bold text-xs text-gray-900">Real places</div>
                <div className="text-[9px] font-medium text-gray-400 mt-1">OpenStreetMap</div>
              </div>
              <div className="bg-gray-50 border border-gray-100 rounded-2xl p-4 flex flex-col items-center justify-center text-center hover:bg-white hover:shadow-lg transition-all group">
                <div className="w-12 h-12 bg-white rounded-xl shadow-sm flex items-center justify-center text-gray-400 group-hover:text-yellow-500 transition-colors mb-3">
                  <IconSun size="1.5em"/>
                </div>
                <div className="font-bold text-xs text-gray-900">Weather</div>
                <div className="text-[9px] font-medium text-gray-400 mt-1">Open-Meteo</div>
              </div>
              <div className="bg-gray-50 border border-gray-100 rounded-2xl p-4 flex flex-col items-center justify-center text-center hover:bg-white hover:shadow-lg transition-all group">
                <div className="w-12 h-12 bg-white rounded-xl shadow-sm flex items-center justify-center text-gray-400 group-hover:text-purple-500 transition-colors mb-3">
                  <IconTicket size="1.5em"/>
                </div>
                <div className="font-bold text-xs text-gray-900">Live web</div>
                <div className="text-[9px] font-medium text-gray-400 mt-1">Tavily</div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* -- Final CTA ------------------------------------------------------------ */}
      <section className="mt-12 max-w-[1200px] mx-auto">
        <div className="bg-gradient-to-r from-[#e85d2c] via-[#db2777] to-[#7c3aed] rounded-[2rem] p-16 relative overflow-hidden text-center shadow-xl">
          {/* Decorative SVG Globe/Lines */}
          <div className="absolute top-1/2 left-0 w-full h-[120px] -translate-y-1/2 opacity-20 pointer-events-none">
            <svg viewBox="0 0 1000 120" className="w-full h-full">
              <path d="M-100,60 Q100,-40 300,60 T700,60 T1100,60" fill="none" stroke="white" strokeWidth="2" strokeDasharray="6 6"/>
              <circle cx="100" cy="60" r="40" fill="none" stroke="white" strokeWidth="2"/>
              <path d="M60,60 h80 M100,20 v80 M75,35 q25,25 50,0 M75,85 q25,-25 50,0" fill="none" stroke="white" strokeWidth="2"/>
            </svg>
          </div>
          
          <div className="absolute right-[15%] top-1/2 -translate-y-1/2 text-white/80 rotate-12 drop-shadow-lg">
            <IconPlane size="4em"/>
          </div>

          <div className="relative z-10 flex flex-col items-center">
            <h2 className="font-display text-4xl sm:text-5xl font-bold tracking-tight text-white mb-4 drop-shadow-md">
              Where are you going?
            </h2>
            <p className="text-lg text-white/90 font-medium mb-10 max-w-md">
              Name a place — and let the agent handle the rest.
            </p>
            <Link
              href={href}
              className="bg-white text-[#e85d2c] text-lg font-bold px-10 py-4 rounded-xl shadow-[0_10px_25px_rgba(0,0,0,0.15)] hover:shadow-[0_15px_35px_rgba(0,0,0,0.25)] hover:scale-105 transition-all flex items-center gap-2"
            >
              {signedIn === null ? "Continue" : "Open the planner"} <IconChevron size="1.2em" />
            </Link>
          </div>
        </div>
      </section>
    </div>
  );
}
