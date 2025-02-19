"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  LiveKitRoom,
  useVoiceAssistant,
  BarVisualizer,
  RoomAudioRenderer,
  VoiceAssistantControlBar,
  AgentState,
  DisconnectButton,
  useLocalParticipant,
} from "@livekit/components-react";
import { useCallback, useEffect, useState } from "react";
import { MediaDeviceFailure } from "livekit-client";
import type { ConnectionDetails } from "./api/connection-details/route";
import { NoAgentNotification } from "@/components/NoAgentNotification";
import { CloseIcon } from "@/components/CloseIcon";
import { useKrispNoiseFilter } from "@livekit/components-react/krisp";
import { Hand } from "lucide-react";

import { Description, Field, Input, Label } from '@headlessui/react'
import { Dialog, DialogPanel, DialogTitle, Button} from '@headlessui/react'
import clsx from 'clsx'


const VALID_CODES = {
  SQUARE: "User Led Interaction",
  CIRCLE: "Agent Led Interaction",
  TRIANGLE: "User Raise Hand"
};

export default function Page() {
  const [connectionDetails, updateConnectionDetails] = useState<ConnectionDetails | undefined>(undefined);
  const [agentState, setAgentState] = useState<AgentState>("disconnected");
  const [code, setCode] = useState("");
  const [mode, setMode] = useState("");
  const [showCodeEntry, setShowCodeEntry] = useState(true);
  const [isHandRaised, setIsHandRaised] = useState(false);
  const [isDialogOpen, setIsDialogOpen] = useState(false);

  const handleCodeSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const upperCode = code.toUpperCase();
    if (VALID_CODES[upperCode as keyof typeof VALID_CODES]) {
      setMode(VALID_CODES[upperCode as keyof typeof VALID_CODES]);
      setShowCodeEntry(false);
    } else {
      setIsDialogOpen(true);
    }
  };

  useEffect(() => {
    if (isDialogOpen) {
      const timer = setTimeout(() => {
        setIsDialogOpen(false);
      }, 4000);
      return () => clearTimeout(timer);
    }
  }, [isDialogOpen]);

  const onConnectbuttonClicked = useCallback(async () => {
    const url = new URL(
      process.env.NEXT_PUBLIC_CONN_DETAILS_ENDPOINT ?? "/api/connection-details",
      window.location.origin
    );
    const response = await fetch(url.toString());
    const connectionDetailsData = await response.json();
    updateConnectionDetails(connectionDetailsData);
  }, []);

  return (
    <main data-lk-theme="default" className="h-full grid content-center bg-[var(--lk-bg)]">
      <AnimatePresence>
        {isDialogOpen && (
          <Dialog static open={isDialogOpen} as={motion.div} className="relative z-50" onClose={() => setIsDialogOpen(false)}>
            <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 20, transition: { duration: 0.2 } }} className="fixed bottom-4 inset-x-0 mx-auto w-fit z-50">
              <DialogPanel as={motion.div} className="max-w-[280px] rounded-lg bg-white/5 px-3 py-2 backdrop-blur-2xl shadow-lg border border-white/10">
                <div className="flex items-center gap-2">
                  <motion.span initial={{ scale: 0 }} animate={{ scale: 1 }} className="text-xl">🚫</motion.span>
                  <div className="flex-1">
                    <DialogTitle as="h3" className="text-sm font-medium text-white">Invalid Code</DialogTitle>
                    <p className="text-xs text-white/60">Please enter a valid code</p>
                  </div>
                  <motion.button whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }} className="text-white/50 hover:text-white/75 p-1 -mr-1" onClick={() => setIsDialogOpen(false)}>
                    <span className="sr-only">Close</span>
                    <svg xmlns="http://www.w3.org/2000/svg" className="h-3.5 w-3.5" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </motion.button>
                </div>
              </DialogPanel>
            </motion.div>
          </Dialog>
        )}
      </AnimatePresence>
      {showCodeEntry ? (
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="w-full max-w-lg flex justify-center items-center mx-auto">
          <form onSubmit={handleCodeSubmit}>
            <Field>
              <Label className="text-lg font-medium text-white">Code</Label>
              <Description className="text-base text-white/50">Please enter the code you were given</Description>
              <Input
              type="text"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoComplete="off"
              className={clsx(
                'mt-3 block w-full rounded-lg border-none bg-white/5 py-1.5 px-3 text-lg text-white',
                'focus:outline-none data-[focus]:outline-2 data-[focus]:-outline-offset-2 data-[focus]:outline-white/25'
              )}
              />
            </Field>
          </form>
        </motion.div>

      ) : (
        <div>
          <LiveKitRoom
            token={connectionDetails?.participantToken}
            serverUrl={connectionDetails?.serverUrl}
            connect={connectionDetails !== undefined}
            audio={true}
            video={false}
            onMediaDeviceFailure={onDeviceFailure}
            onDisconnected={() => {
              updateConnectionDetails(undefined);
            }}
            className="grid grid-rows-[2fr_1fr] items-center"
          >
            <SimpleVoiceAssistant 
              onStateChange={setAgentState} 
              isHandRaised={isHandRaised}
              mode={mode}
            />
            <ControlBar
              onConnectbuttonClicked={onConnectbuttonClicked}
              agentState={agentState}
              mode={mode}
              isHandRaised={isHandRaised}
              setIsHandRaised={setIsHandRaised}
            />
            <RoomAudioRenderer />
            <NoAgentNotification state={agentState} />
          </LiveKitRoom>
          <p className="text-white/60 text-center"><i>{mode}</i></p>
        </div>
      )}
    </main>
  );
}

function SimpleVoiceAssistant(props: {
  onStateChange: (state: AgentState) => void;
  isHandRaised: boolean;
  mode: string;
}) {
  const { state, audioTrack } = useVoiceAssistant();
  const { microphoneTrack } = useLocalParticipant();
  
  useEffect(() => {
    props.onStateChange(state);
  }, [props, state]);

  // Control microphone based on hand raise state
  useEffect(() => {
    if (props.mode === "User Raise Hand" && microphoneTrack?.track) {
      if (props.isHandRaised) {
        microphoneTrack.track.enable();
      } else {
        microphoneTrack.track.disable();
      }
    }
  }, [props.isHandRaised, props.mode, microphoneTrack]);

  return (
    <div className="h-[300px] max-w-[90vw] mx-auto">
      <BarVisualizer
        state={state}
        barCount={5}
        trackRef={audioTrack}
        className="agent-visualizer"
        options={{ minHeight: 24 }}
      />
    </div>
  );
}

function ControlBar(props: {
  onConnectbuttonClicked: () => void;
  agentState: AgentState;
  mode: string;
  isHandRaised: boolean;
  setIsHandRaised: (raised: boolean) => void;
}) {
  const krisp = useKrispNoiseFilter();
  const { localParticipant } = useLocalParticipant();

  useEffect(() => {
    krisp.setNoiseFilterEnabled(true);
  }, []);

  // Handle hand raise toggle
  const toggleHand = useCallback(() => {
    props.setIsHandRaised(!props.isHandRaised);
  }, [props.isHandRaised, props.setIsHandRaised]);

  return (
    <div className="relative h-[100px]">
      <AnimatePresence>
        {props.agentState === "disconnected" && (
          <motion.button
            initial={{ opacity: 0, top: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0, top: "-10px" }}
            transition={{ duration: 1, ease: [0.09, 1.04, 0.245, 1.055] }}
            className="uppercase absolute left-1/2 -translate-x-1/2 px-4 py-2 bg-white text-black rounded-md"
            onClick={() => props.onConnectbuttonClicked()}
          >
            Start Learning
          </motion.button>
        )}
      </AnimatePresence>
      <AnimatePresence>
        {props.agentState !== "disconnected" && props.agentState !== "connecting" && (
          <motion.div
            initial={{ opacity: 0, top: "10px" }}
            animate={{ opacity: 1, top: 0 }}
            exit={{ opacity: 0, top: "-10px" }}
            transition={{ duration: 0.4, ease: [0.09, 1.04, 0.245, 1.055] }}
            className="flex h-8 absolute left-1/2 -translate-x-1/2 justify-center items-center gap-4"
          >
            {props.mode === "User Raise Hand" && (
              <button
                onClick={toggleHand}
                className={`p-2 rounded transition-colors ${props.isHandRaised ? 'bg-green-500' : 'bg-gray-500'}`}
                title={props.isHandRaised ? "Lower Hand" : "Raise Hand"}
              >
                <Hand className="w-5 h-5 text-white" />
              </button>
            )}
            <VoiceAssistantControlBar controls={{ leave: false }} />
            <DisconnectButton>
              <CloseIcon />
            </DisconnectButton>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function onDeviceFailure(error?: MediaDeviceFailure) {
  console.error(error);
  alert(
    "Error acquiring camera or microphone permissions. Please make sure you grant the necessary permissions in your browser and reload the tab"
  );
}